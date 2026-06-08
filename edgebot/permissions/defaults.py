"""
edgebot/permissions/defaults.py - Single source of truth for bash policy.

Both PermissionManager and the lower-level run_bash guard pull their
deny patterns from this file. To add or tighten a rule, edit one of the
category lists below; do not duplicate the regex in tools/shell.py.

Categories:
  DESTRUCTIVE_PATTERNS    - irreversible filesystem/disk/system actions
  REMOTE_EXEC_PATTERNS    - curl|sh and similar pipe-to-interpreter idioms
  INTERACTIVE_TUI_PATTERNS- TUI/REPL programs that block on a tty
  INPLACE_EDIT_PATTERNS   - sed -i / perl -i / ruby -i (bypass file_state)
  DANGEROUS_SYSTEM_PATTERNS - account, init, cron wipe, signal-init etc.

INTERPRETER_PROGRAMS lists languages/build tools that must always require
an explicit per-command approval, even if their bare name appears in the
allowlist (Python is Turing-complete; allowing "python" wholesale defeats
the rest of the policy).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Program allowlist (default seed for `bash_programs` rule)
#
# Only commands whose full surface area is *narrow and read-mostly* belong
# here. Interpreters, package managers, and build tools live in
# INTERPRETER_PROGRAMS instead and are forced through the prompt flow.
# ---------------------------------------------------------------------------
DEFAULT_BASH_PROGRAMS: list[str] = [
    "ls", "cat", "pwd", "echo",
    "rg", "grep", "find",
    "head", "tail", "wc",
    "which", "where", "type", "file",
    "git", "diff",
    "true", "false",
    # Shell chain helper — chained commands are validated individually.
    "for",
]

# Interpreters / build drivers / package managers. Even if the user has
# previously granted "allow program: python", PermissionManager treats these
# as sensitive and asks again, recommending an `allow_prefix` (exact command)
# scope rather than a bare program-name grant.
INTERPRETER_PROGRAMS: frozenset[str] = frozenset({
    # Language runtimes
    "python", "python3", "py",
    "node", "deno", "bun",
    "ruby", "perl", "php", "lua", "tcl",
    # Package managers / installers
    "pip", "pip3", "pipx", "uv",
    "npm", "npx", "yarn", "pnpm",
    "gem", "bundle", "bundler",
    "cargo", "rustup",
    "go",
    # Build drivers
    "make", "cmake", "ninja", "bazel", "buck",
    # Shells (running a sub-shell with arbitrary script)
    "bash", "sh", "zsh", "fish", "ksh", "dash",
    "powershell", "pwsh", "cmd",
})

# ---------------------------------------------------------------------------
# Deny patterns — applied as case-insensitive regex search on the command
# string. Group by intent so the categories can be tuned independently.
# ---------------------------------------------------------------------------

DESTRUCTIVE_PATTERNS: list[str] = [
    r"\brm\s+-[rf]{1,2}\s+/(?!\S*(?:tmp|var/tmp)/)",   # rm -rf / (allow /tmp/*)
    r"\brm\s+-[rf]{1,2}\s+~",                          # rm -rf ~
    r"\brm\s+-[rf]{1,2}\s+\$home",                     # rm -rf $HOME
    r"\bdel\s+/[fq]\b",                                # del /f, del /q
    r"\brmdir\s+/s\b",                                 # rmdir /s
    r"(?:^|[;&|]\s*)format(?!=)(?!-)\b",               # bare `format` only
    r"\b(?:mkfs|diskpart|fdisk|parted|wipefs)\b",
    r"\bdd\b[^|;&<>]*\b(?:if|of)=",
    r">\s*/dev/sd",
    r"\bchmod\s+-R\s+0?777\s+/",
    r"\b(?:shutdown|reboot|poweroff|halt)\b",
    r"\binit\s+[06]\b",
    r":\(\)\s*\{.*\};\s*:",                            # fork bomb
]

REMOTE_EXEC_PATTERNS: list[str] = [
    # curl/wget/fetch piped into an interpreter
    r"\b(?:curl|wget|fetch)\b[^|;&<>]+\|\s*(?:bash|sh|zsh|ksh|fish|python3?|py|node|ruby|perl|pwsh|powershell)\b",
    # PowerShell IEX/Invoke-Expression around a downloader
    r"\b(?:iex|invoke-expression)\b\s*\(",
]

# Each subpattern uses (?![\w.-]) at the end so that `vim` does not match
# `vimdiff`, `ssh` does not match `ssh-keygen`, `top` does not match
# `htop`, etc. Anchors at the front (^ or chain operator) keep these from
# misfiring on substrings like `mytop` either.
INTERACTIVE_TUI_PATTERNS: list[str] = [
    # Editors
    r"(?:^|[;&|]\s*)(?:vi|vim|nvim|view|nano|emacs|emacsclient|pico|joe|micro)(?![\w.-])",
    # Pagers — also catches `cmd | less` since the agent has no tty
    r"(?:^|[;&|]\s*)(?:less|more|most)(?![\w.-])",
    # Live system monitors
    r"(?:^|[;&|]\s*)(?:top|htop|btop|atop|iftop|iotop|nethogs|glances)(?![\w.-])",
    # Interactive networking (sshpass / ssh-keygen are intentionally allowed)
    r"(?:^|[;&|]\s*)(?:ssh|telnet|sftp|ftp|mosh|tmux|screen|byobu)(?![\w.-])",
    # Database REPLs (allow inline -c/-e/-f variants)
    r"(?:^|[;&|]\s*)(?:mysql|psql|sqlite3|mongo|mongosh|redis-cli|cqlsh)(?![\w.-])(?![^|;&<>]*\s+(?:-c|--command|-e|--execute|-f|--file))",
    # Bare REPL launches: `python`, `node`, `ruby` with no script/args
    r"(?:^|[;&|]\s*)(?:python|python3|py|node|deno|bun|ruby|irb|php|lua)\s*$",
    # Debuggers in interactive mode
    r"(?:^|[;&|]\s*)(?:gdb|lldb|pdb|pry)(?![\w.-])(?![^|;&<>]*\s+(?:--batch|-batch))",
]

INPLACE_EDIT_PATTERNS: list[str] = [
    # sed -i, sed -i.bak, sed --in-place, sed -e ... -i ...
    r"\bsed\b[^|;&<>]*\s-i(?:\.\S+)?\b",
    r"\bsed\b[^|;&<>]*\s--in-place(?:=\S*)?\b",
    # perl -i / -pi / -pie etc. — any cluster flag containing 'i'
    r"\bperl\s+-[a-z]*i\b",
    # ruby -i / -pi
    r"\bruby\s+-[a-z]*i\b",
]

DANGEROUS_SYSTEM_PATTERNS: list[str] = [
    r"\bkill\s+-9?\s+1\b",                  # kill PID 1
    r"\bpkill\s+-9?\s+-1\b",                # pkill -1 (signal init)
    r"\b(?:userdel|usermod|passwd|chpasswd)\b",
    r"\bcrontab\s+-r\b",
    r"\biptables\s+-F\b",                   # iptables flush
    r"\bnft\s+flush\s+ruleset\b",
]

# Master deny list assembled from the categories above.
DEFAULT_BASH_DENY_PATTERNS: list[str] = (
    DESTRUCTIVE_PATTERNS
    + REMOTE_EXEC_PATTERNS
    + INTERACTIVE_TUI_PATTERNS
    + INPLACE_EDIT_PATTERNS
    + DANGEROUS_SYSTEM_PATTERNS
)
