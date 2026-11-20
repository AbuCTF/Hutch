#!/bin/sh
# curl -fsSL hutch.abu.rocks | sh
# env: HUTCH_REF (branch/tag, default master), HUTCH_HOME (default ~/.hutch), HUTCH_BIN (default ~/.local/bin),
#      HUTCH_SRC (package url, default the github tarball for HUTCH_REF)
set -eu

main() {
    ref="${HUTCH_REF:-master}"
    home="${HUTCH_HOME:-$HOME/.hutch}"
    bin="${HUTCH_BIN:-$HOME/.local/bin}"
    src="${HUTCH_SRC:-https://github.com/AbuCTF/Hutch/archive/$ref.tar.gz}"
    venv="$home/venv"

    say() { printf '\033[1m%s\033[0m\n' "$*"; }
    die() { printf 'error: %s\n' "$*" >&2; exit 1; }

    command -v python3 >/dev/null || die "python3 not found"
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "python 3.10+ required"
    python3 -c 'import venv, ensurepip' 2>/dev/null \
        || die "python venv missing, install python3-venv"

    say "installing hutch $ref into $venv"
    [ -x "$venv/bin/python" ] || python3 -m venv "$venv"
    "$venv/bin/python" -m pip install -q --upgrade pip
    "$venv/bin/python" -m pip install -q --upgrade "hutch[mcp] @ $src"

    mkdir -p "$bin"
    ln -sf "$venv/bin/hutch" "$bin/hutch"
    ln -sf "$venv/bin/hutch-mcp" "$bin/hutch-mcp"

    chrome=$("$venv/bin/python" -c 'from hutch.browser import find_executable
try: print(find_executable("chrome"))
except Exception: pass' 2>/dev/null || true)
    if [ -n "$chrome" ]; then
        say "using Google Chrome at $chrome"
        exe="$chrome"
    else
        say "Google Chrome not found (hutch's default browser); fetching playwright's chromium as a fallback"
        echo "  install google-chrome-stable for the best results, or pass --browser chromium"
        "$venv/bin/python" -m playwright install chromium --no-shell
        exe=$("$venv/bin/python" -c 'from hutch.browser import find_executable; print(find_executable("chromium"))')
    fi

    say "checking system libraries"
    if [ "$(uname)" = "Linux" ]; then
        if ldd "$exe" 2>/dev/null | grep -q "not found"; then
            if command -v apt-get >/dev/null; then
                if [ "$(id -u)" -eq 0 ]; then
                    "$venv/bin/playwright" install-deps chromium
                else
                    sudo "$venv/bin/playwright" install-deps chromium </dev/tty
                fi
            else
                echo "  the browser is missing libraries, hutch doctor lists them"
            fi
        else
            echo "  ok"
        fi
        command -v Xvfb >/dev/null || echo "  optional: Xvfb for --display virtual (pacman -S xorg-server-xvfb | apt install xvfb | dnf install xorg-x11-server-Xvfb)"
    fi

    echo
    "$venv/bin/hutch" doctor || true

    case ":$PATH:" in
        *":$bin:"*) ;;
        *) printf '\nadd %s to PATH:\n  echo '\''export PATH="%s:$PATH"'\'' >> ~/.bashrc && exec bash\n' "$bin" "$bin" ;;
    esac
    printf '\nnext: hutch create hello --url https://example.com\n'
}

main "$@"
