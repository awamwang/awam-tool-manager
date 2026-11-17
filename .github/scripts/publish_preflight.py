#!/usr/bin/env python3
"""Public-release preflight: secrets, personal paths, dev journals, identity.

Read-only. Produces a Chinese Markdown report and optional JSON.

Exit codes: 0 clean / 1 blocking findings / 2 usage or environment error.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
SEV_LABEL = {"critical": "阻断", "high": "高", "medium": "中", "low": "低", "info": "提示"}
BLOCKING = {"critical", "high"}

DEFAULT_MAX_FILE_MB = 5

EMAIL_ALLOWED = (
    "example.com", "example.org", "example.net", "test.com", "localhost",
    "invalid", "your-domain.com", "domain.com",
)


@dataclass
class Finding:
    rule: str
    severity: str
    title: str
    fix: str
    hits: list[tuple[str, int, str]] = field(default_factory=list)  # path, line, evidence
    note: str = ""


@dataclass
class Rule:
    rule: str
    severity: str
    title: str
    fix: str
    pattern: re.Pattern
    skip_paths: tuple[str, ...] = ()


TEXT_RULES: list[Rule] = [
    Rule(
        "win_home", "high", "本机用户目录绝对路径",
        "改成 %USERPROFILE% / <用户目录> 占位，或从配置读取。",
        re.compile(r"""(?i)\b[a-z]:[\\/]{1,2}Users[\\/]{1,2}([^\\/\s"'`<>|,;)]+)"""),
    ),
    Rule(
        "posix_home", "high", "本机用户主目录路径",
        "改成 ~ / $HOME，或 <用户目录> 占位。",
        re.compile(r"""(?<![\w.])/(?:home|Users)/([A-Za-z0-9._-]+)"""),
    ),
    Rule(
        "drive_path", "medium", "绝对盘符路径",
        "文档/脚本里的绝对路径改成相对项目根或占位符，换机器才能跑。",
        re.compile(
            r"""(?i)(?<![\w])[a-z]:(?:[\\/]{1,2}(?!(?:windows|program files|programdata|users)[\\/])"""
            r"""[^\s"'`<>|,;)\\/]+)+"""
        ),
        skip_paths=("*.gitignore", "*.lock", "*.min.js"),
    ),
    Rule(
        "email", "medium", "邮箱地址",
        "个人邮箱改占位或移除；确实是联络方式就保留并在报告里标注。",
        re.compile(r"""\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"""),
        skip_paths=("*.svg", "*.min.js", "*.lock"),
    ),
    Rule(
        "cn_phone", "high", "手机号/座机",
        "删除或打码。",
        re.compile(r"""(?<!\d)(?:1[3-9]\d{9}|0\d{2,3}-\d{7,8})(?!\d)"""),
    ),
    Rule(
        "secret_literal", "critical", "疑似密钥字面量",
        "立刻吊销该密钥并改走环境变量；若已进历史需重写历史或换新仓库。",
        re.compile(
            r"""(?x)
            (?: sk-[A-Za-z0-9]{20,}
              | AKIA[0-9A-Z]{16}
              | LTAI[0-9A-Za-z]{12,}
              | gh[pousr]_[A-Za-z0-9]{20,}
              | xox[baprs]-[A-Za-z0-9-]{10,}
              | AIza[0-9A-Za-z_\-]{30,}
              | -----BEGIN\ (?:RSA\ |EC\ |OPENSSH\ |PGP\ )?PRIVATE\ KEY-----
            )"""
        ),
    ),
    Rule(
        "secret_kv", "high", "配置里的高熵赋值",
        "确认是真密钥还是测试假值；真密钥立刻吊销并改环境变量。",
        re.compile(
            r"""(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?key|client[_-]?secret|auth[_-]?token|password)\b"""
            r"""\s*[:=]\s*["'][A-Za-z0-9_\-+/=]{16,}["']"""
        ),
        skip_paths=("test_*.py", "*_test.py", "tests/*", "*/tests/*"),
    ),
    Rule(
        "rfc1918", "medium", "内网 IP",
        "换成 127.0.0.1 / 占位；内网地址会暴露拓扑。",
        re.compile(
            r"""\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}"""
            r"""|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"""
        ),
    ),
    Rule(
        "internal_host", "medium", "内网域名",
        "移除或改 example.com。",
        re.compile(r"""\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:internal|intranet|corp|lan)\b""", re.I),
    ),
]

JUNK_PATHS: list[tuple[str, str, str, str]] = [
    # pattern, severity, title, fix
    (".scratch*", "high", "本地草稿目录被跟踪", "git rm -r --cached 并从 .gitignore 确认已忽略。"),
    ("*/.scratch/*", "high", "本地草稿目录被跟踪", "git rm -r --cached 并从 .gitignore 确认已忽略。"),
    (".workbuddy/*", "high", "AI 会话笔记被跟踪", "开发日志含机器路径与未定事项，发布前摘出。"),
    ("*/.workbuddy/*", "high", "AI 会话笔记被跟踪", "开发日志含机器路径与未定事项，发布前摘出。"),
    ("*.log", "medium", "日志文件被跟踪", "git rm --cached 并加入 .gitignore。"),
    ("*.bak*", "medium", "备份文件被跟踪", "git rm --cached；备份不该进版本库。"),
    ("*.orig", "medium", "合并残留被跟踪", "git rm --cached。"),
    (".DS_Store", "low", "系统垃圾文件", "git rm --cached 并加入 .gitignore。"),
    ("Thumbs.db", "low", "系统垃圾文件", "git rm --cached 并加入 .gitignore。"),
    (".idea/*", "low", "IDE 目录被跟踪", "git rm -r --cached。"),
    (".vscode/*", "low", "IDE 目录被跟踪", "只保留可共享的 settings；其余摘出。"),
    ("__pycache__/*", "low", "编译缓存被跟踪", "git rm -r --cached。"),
    ("*.pyc", "low", "编译缓存被跟踪", "git rm --cached。"),
]

BINARY_EXT = {
    ".wav", ".mp3", ".m4a", ".flac", ".mp4", ".mov", ".mkv", ".avi",
    ".zip", ".7z", ".rar", ".gz", ".exe", ".dll", ".so", ".dylib",
    ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".woff", ".woff2",
}


def run(cmd: list[str], cwd: Path, binary: bool = False) -> tuple[int, bytes]:
    try:
        p = subprocess.run(cmd, cwd=str(cwd), capture_output=True)
    except FileNotFoundError:
        return 127, b""
    return p.returncode, p.stdout if binary else p.stdout.decode("utf-8", "replace")


def git(root: Path, *args: str) -> str:
    code, out = run(["git", *args], root)
    if code != 0:
        raise SystemExit(f"git {' '.join(args)} failed:\n{out}")
    return out


def git_z(root: Path, *args: str) -> list[str]:
    code, out = run(["git", *args], root, binary=True)
    if code != 0:
        raise SystemExit(f"git {' '.join(args)} failed")
    return [p for p in out.decode("utf-8", "replace").split("\0") if p]


def find_gitleaks() -> str | None:
    exe = shutil.which("gitleaks")
    if exe:
        return exe
    local = os.environ.get("LOCALAPPDATA", "")
    candidates: list[Path] = []
    if local:
        candidates += list(Path(local, "Microsoft", "WinGet", "Packages").glob("Gitleaks.Gitleaks_*/gitleaks.exe"))
    candidates += [
        Path.home() / "scoop" / "apps" / "gitleaks" / "current" / "gitleaks.exe",
        Path("/usr/local/bin/gitleaks"),
        Path("/usr/bin/gitleaks"),
    ]
    for c in candidates:
        if Path(c).is_file():
            return str(c)
    return None


def load_config(root: Path) -> dict:
    cfg_path = root / "publish-check.json"
    if not cfg_path.is_file():
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"publish-check.json 解析失败: {exc}")


def is_allowed(cfg: dict, rel: str, rule: str) -> bool:
    for entry in cfg.get("allow", []):
        pat, _, r = entry.partition(":")
        if fnmatch.fnmatch(rel, pat) and (not r or r == rule):
            return True
    return False


def read_text(path: Path, max_bytes: int) -> str | None:
    try:
        if path.stat().st_size > max_bytes:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\0" in raw[:8192]:
        return None
    return raw.decode("utf-8", "replace")


def scan_text(files: list[str], root: Path, cfg: dict, max_bytes: int) -> list[Finding]:
    by_rule: dict[str, Finding] = {r.rule: Finding(r.rule, r.severity, r.title, r.fix) for r in TEXT_RULES}
    terms = [t for t in cfg.get("self_terms", []) if t]
    if terms:
        by_rule["self_term"] = Finding(
            "self_term", "high", "自定义个人标识命中",
            "替换为占位；若是公开账号名且你接受暴露，加进 publish-check.json 的 allow。",
        )

    for rel in files:
        text = read_text(root / rel, max_bytes)
        if text is None:
            continue
        lines = text.splitlines()
        for rule in TEXT_RULES:
            if is_allowed(cfg, rel, rule.rule):
                continue
            if any(fnmatch.fnmatch(rel, p) for p in rule.skip_paths):
                continue
            for lineno, line in enumerate(lines, 1):
                for m in rule.pattern.finditer(line):
                    ev = m.group(0)
                    if rule.rule == "email" and ev.split("@")[-1].lower() in EMAIL_ALLOWED:
                        continue
                    by_rule[rule.rule].hits.append((rel, lineno, ev))

        if terms:
            low = text.lower()
            for term in terms:
                if term.lower() not in low:
                    continue
                for lineno, line in enumerate(lines, 1):
                    if term.lower() in line.lower():
                        safe = line.strip()[:160]
                        by_rule["self_term"].hits.append((rel, lineno, safe))
    return [f for f in by_rule.values() if f.hits]


def scan_junk(tracked: list[str], root: Path, max_mb: int) -> list[Finding]:
    out: dict[str, Finding] = {}
    for rel in tracked:
        for pat, sev, title, fix in JUNK_PATHS:
            if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, pat.lstrip("./")):
                f = out.setdefault(pat, Finding(f"junk:{pat}", sev, title, fix))
                f.hits.append((rel, 0, ""))
                break
        suffix = Path(rel).suffix.lower()
        try:
            size_mb = (root / rel).stat().st_size / (1024 * 1024)
        except OSError:
            size_mb = 0
        if suffix in BINARY_EXT and size_mb >= max_mb:
            f = out.setdefault("big_binary", Finding(
                "junk:big_binary", "medium", f"大于 {max_mb}MB 的二进制资源",
                "确认是必要素材；否则换成外链或示例小样。",
            ))
            f.hits.append((rel, 0, f"{size_mb:.1f} MB"))
    return list(out.values())


def scan_hygiene(tracked: list[str], root: Path) -> Finding:
    names = {Path(p).name.lower() for p in tracked}
    tops = {p.split("/")[0].lower() for p in tracked}
    missing = []
    if not any(n.startswith("readme") for n in names):
        missing.append("README")
    if not any(n.startswith("license") or n.startswith("licence") for n in names):
        missing.append("LICENSE")
    if ".gitignore" not in tops and ".gitignore" not in names:
        missing.append(".gitignore")
    f = Finding("hygiene", "low", "缺少发布常用文件", "公开仓库建议补齐。")
    for m in missing:
        f.hits.append((m, 0, "缺失"))
    return f


def scan_identity(root: Path, cfg: dict) -> list[Finding]:
    findings: list[Finding] = []
    code, out = run(["git", "log", "--all", "--format=%an <%ae>|%cn <%ce>"], root)
    ids = set()
    if code == 0:
        for line in out.splitlines():
            if "|" in line:
                a, c = line.split("|", 1)
                ids.add(a)
                ids.add(c)
    if ids:
        f = Finding("identity", "medium", "提交身份会随历史公开",
                    "不想暴露就用 noreply 邮箱；已推送的历史需重写或新建干净仓库。")
        for i, ident in enumerate(sorted(ids)):
            f.hits.append(("git log --all", 0, ident))
        findings.append(f)

    code, remotes = run(["git", "remote", "-v"], root)
    if code == 0 and remotes.strip():
        f = Finding("remote_token", "critical", "remote 地址里带了凭据",
                    "立刻改 remote 并在平台吊销该 token。")
        for line in remotes.splitlines():
            if re.search(r"://[^/\s]+:[^/@\s]+@", line):
                f.hits.append(("git remote -v", 0, re.sub(r"://[^/\s]+:[^/@\s]+@", "://***:***@", line)))
        if f.hits:
            findings.append(f)
        f2 = Finding("remote", "info", "remote 地址（发布后公开可见）", "确认目标仓库是你想要的那个。")
        for line in remotes.splitlines():
            f2.hits.append(("git remote -v", 0, line.strip()))
        findings.append(f2)
    return findings


def scan_gitleaks(root: Path, exe: str, logs: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    with tempfile.TemporaryDirectory(prefix="preflight-") as tmp:
        report = Path(tmp) / "gitleaks.json"
        cmd = [exe, "git", "--no-banner", "--redact", "--exit-code", "0",
               "--report-format", "json", "--report-path", str(report), "."]
        proc = subprocess.run(cmd, cwd=str(root), capture_output=True,
                              encoding="utf-8", errors="replace")
        code = proc.returncode
        for line in (proc.stdout + proc.stderr).splitlines():
            if "no leaks found" in line or "commits scanned" in line:
                msg = line.strip().split("INF")[-1].strip()
                logs.append(f"gitleaks: {msg}")
        if code not in (0, 1):
            findings.append(Finding("gitleaks", "info", "gitleaks 执行异常", "检查 gitleaks 版本与参数。",
                                    note=(proc.stderr or proc.stdout).strip()[:500]))
            return findings
        if not report.is_file():
            return findings
        try:
            data = json.loads(report.read_text(encoding="utf-8") or "[]")
        except json.JSONDecodeError:
            return findings
        if not data:
            return findings
        f = Finding("gitleaks", "critical", "gitleaks 判定为密钥泄漏",
                    "吊销密钥；已进历史只能重写历史或换新仓库，删文件不解决问题。")
        for item in data[:200]:
            loc = f"{item.get('File', '?')}:{item.get('StartLine', 0)}"
            commit = (item.get("Commit") or "")[:7]
            note = f"{item.get('RuleID', '')}" + (f" @{commit}" if commit else "")
            f.hits.append((loc, item.get("StartLine", 0), f"{item.get('Match', '')} [{note}]"))
        findings.append(f)
    return findings


def render_md(root: Path, findings: list[Finding], logs: list[str], stats: dict) -> str:
    findings.sort(key=lambda f: (SEV_ORDER[f.severity], f.rule))
    blocking = [f for f in findings if f.severity in BLOCKING]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# 发布前检查报告：{root.name}", "", f"生成时间：{now}　路径：`{root}`", ""]

    verdict = "❌ 有阻断项，先处理再发布" if blocking else "✅ 未发现阻断项"
    lines += [f"**结论**：{verdict}", ""]
    lines += ["| 项目 | 数值 |", "| --- | --- |"]
    for k, v in stats.items():
        lines.append(f"| {k} | {v} |")
    lines += ["", "## 汇总", ""]
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + len(f.hits)
    lines += ["| 等级 | 命中数 |", "| --- | --- |"]
    for sev in ("critical", "high", "medium", "low", "info"):
        if sev in counts:
            lines.append(f"| {SEV_LABEL[sev]} | {counts[sev]} |")
    lines.append("")

    if not findings:
        lines += ["未发现需要处理的项。", ""]

    for sev in ("critical", "high", "medium", "low"):
        group = [f for f in findings if f.severity == sev]
        if not group:
            continue
        lines += [f"## {SEV_LABEL[sev]}项（{sev}）", ""]
        for f in group:
            lines.append(f"### {f.title}")
            lines.append("")
            lines.append(f"- 规则：`{f.rule}`　命中：{len(f.hits)} 处")
            lines.append(f"- 修复：{f.fix}")
            if f.note:
                lines.append(f"- 备注：{f.note}")
            lines.append("")
            shown = f.hits[:30]
            for path, line, ev in shown:
                loc = f"`{path}`" + (f":{line}" if line else "")
                lines.append(f"  - {loc}" + (f" → `{ev[:180]}`" if ev else ""))
            if len(f.hits) > len(shown):
                lines.append(f"  - ……另有 {len(f.hits) - len(shown)} 处")
            lines.append("")

    info = [f for f in findings if f.severity == "info"]
    if info:
        lines += ["## 提示项", ""]
        for f in info:
            lines.append(f"- **{f.title}**（{f.fix}）")
            for _, _, ev in f.hits[:10]:
                lines.append(f"  - {ev}")
        lines.append("")

    if logs:
        lines += ["## 扫描日志", ""]
        lines += [f"- {l}" for l in logs]
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="仓库公开发布前的隐私/密钥检查")
    ap.add_argument("path", nargs="?", default=".", help="仓库路径，默认当前目录")
    ap.add_argument("--md", help="Markdown 报告输出路径；默认打印到 stdout")
    ap.add_argument("--json", dest="json_out", help="JSON 结果输出路径")
    ap.add_argument("--self-term", action="append", default=[], help="额外的个人标识（姓名/账号/handle），可多次")
    ap.add_argument("--max-file-mb", type=int, default=None, help=f"文本扫描与二进制告警阈值（默认 {DEFAULT_MAX_FILE_MB}）")
    ap.add_argument("--skip-history", action="store_true", help="跳过 gitleaks 全历史扫描")
    ap.add_argument("--exit-zero", action="store_true", help="无论结果如何都返回 0")
    args = ap.parse_args(argv)

    code, out = run(["git", "-C", args.path, "rev-parse", "--show-toplevel"], Path("."))
    if code != 0 or not out.strip():
        print(f"不是 git 仓库: {args.path}", file=sys.stderr)
        return 2
    root = Path(out.strip())
    cfg = load_config(root)
    max_mb = args.max_file_mb or int(cfg.get("max_file_mb", DEFAULT_MAX_FILE_MB))
    cfg = {**cfg, "self_terms": [*cfg.get("self_terms", []), *args.self_term]}

    tracked = git_z(root, "ls-files", "-z")
    untracked = git_z(root, "ls-files", "-z", "--others", "--exclude-standard")
    ignored_tracked = git_z(root, "ls-files", "-z", "-i", "-c", "--exclude-standard")

    findings: list[Finding] = []
    findings += scan_text(tracked + untracked, root, cfg, max_mb * 1024 * 1024)
    findings += scan_junk(tracked, root, max_mb)
    findings += scan_identity(root, cfg)

    if ignored_tracked:
        f = Finding("tracked_ignored", "high", "文件已跟踪但被 .gitignore 忽略",
                    "说明曾强制 add 过：git rm --cached 摘出，历史里若已推送需一并处理。")
        f.hits = [(p, 0, "") for p in ignored_tracked]
        findings.append(f)

    hygiene = scan_hygiene(tracked, root)
    if hygiene.hits:
        findings.append(hygiene)

    logs: list[str] = []
    if args.skip_history:
        logs.append("gitleaks: 已按参数跳过历史扫描")
    else:
        exe = find_gitleaks()
        if exe:
            findings += scan_gitleaks(root, exe, logs)
        else:
            logs.append("gitleaks 未安装，已跳过全历史密钥扫描。安装：winget install Gitleaks.Gitleaks")

    stats = {
        "跟踪文件": len(tracked),
        "未跟踪但会纳入": len(untracked),
        "被忽略却仍跟踪": len(ignored_tracked),
        "提交数": git(root, "rev-list", "--count", "HEAD").strip(),
    }
    md = render_md(root, findings, logs, stats)

    payload = {
        "repo": str(root),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "stats": stats,
        "findings": [
            {"rule": f.rule, "severity": f.severity, "title": f.title, "fix": f.fix,
             "hits": [{"path": p, "line": l, "evidence": e} for p, l, e in f.hits]}
            for f in findings
        ],
    }
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.md:
        Path(args.md).parent.mkdir(parents=True, exist_ok=True)
        Path(args.md).write_text(md, encoding="utf-8")
        print(f"报告已写入 {args.md}")
    else:
        print(md)

    blocking = sum(1 for f in findings if f.severity in BLOCKING)
    print(f"\n阻断项类别：{blocking}", file=sys.stderr)
    if args.exit_zero:
        return 0
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
