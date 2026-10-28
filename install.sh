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
        || die "python venv missing — sudo apt install python3-venv (or your distro's equivalent)"

    say "[1/4] hutch ($ref) -> $venv"
    [ -x "$venv/bin/python" ] || python3 -m venv "$venv"
    "$venv/bin/python" -m pip install -q --upgrade pip
    "$venv/bin/python" -m pip install -q --upgrade "hutch[mcp] @ $src"

    mkdir -p "$bin"
    ln -sf "$venv/bin/hutch" "$bin/hutch"
    ln -sf "$venv/bin/hutch-mcp" "$bin/hutch-mcp"

    say "[2/4] chromium"
    "$venv/bin/python" -m playwright install chromium --no-shell

    say "[3/4] system libraries"
    if [ "$(uname)" = "Linux" ]; then
        exe=$("$venv/bin/python" -c 'from hutch.browser import find_executable; print(find_executable("chromium"))')
        if ldd "$exe" 2>/dev/null | grep -q "not found"; then
            if command -v apt-get >/dev/null; then
                if [ "$(id -u)" -eq 0 ]; then
                    "$venv/bin/playwright" install-deps chromium
                else
                    sudo "$venv/bin/playwright" install-deps chromium </dev/tty
                fi
            else
                echo "  chromium is missing libraries; install them with your package manager ('hutch doctor' lists them)"
            fi
        else
            echo "  already present"
        fi
    fi

    say "[4/4] checking"
    "$venv/bin/hutch" doctor || true

    case ":$PATH:" in
        *":$bin:"*) ;;
        *) printf '\nadd %s to PATH:\n  echo '\''export PATH="%s:$PATH"'\'' >> ~/.bashrc && exec bash\n' "$bin" "$bin" ;;
    esac
    printf '\nnext: hutch login google   (then: hutch --help)\n'
}

main "$@"
