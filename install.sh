#!/usr/bin/env bash
# periodika-agent uzstādīšana uz Linux vai macOS.
#
#   ./install.sh              # virtuālā vide + pamatuzstādīšana + pārbaude
#   ./install.sh --all        # arī attēlu apstrāde un Anthropic SDK
#   ./install.sh --no-venv    # uzstādīt pašreizējā Python vidē
#
# Skripts neko neinstalē ar sudo un neko nedzēš: sistēmas pakotnes (tesseract,
# poppler) tas tikai pārbauda un pasaka, ko palaist.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

EXTRAS=""
USE_VENV=1
BROWSER=0
for arg in "$@"; do
  case "$arg" in
    --all)     EXTRAS="[images,claude,browser]"; BROWSER=1 ;;
    --images)  EXTRAS="[images]" ;;
    --browser) EXTRAS="[browser]"; BROWSER=1 ;;
    --no-venv) USE_VENV=0 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "nezināms arguments: $arg" >&2; exit 2 ;;
  esac
done

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "Nav atrasts python3. Uzstādi Python 3.9 vai jaunāku." >&2
  exit 1
fi
"$PYTHON" - <<'PYCHECK'
import sys
if sys.version_info < (3, 9):
    sys.exit(f"Vajadzīgs Python 3.9+, atrasts {sys.version.split()[0]}")
PYCHECK

if [ "$USE_VENV" -eq 1 ]; then
  if [ ! -d .venv ]; then
    echo "==> Veidoju virtuālo vidi .venv"
    "$PYTHON" -m venv .venv
  fi
  # shellcheck disable=SC1091
  . .venv/bin/activate
  PYTHON="$PWD/.venv/bin/python"
fi

echo "==> Uzstādu periodika-agent$EXTRAS"
"$PYTHON" -m pip install --quiet --upgrade pip
"$PYTHON" -m pip install --quiet -e ".$EXTRAS"

if [ "$BROWSER" -eq 1 ]; then
  echo "==> Lejupielādēju Chromium (SPA lapu lasīšanai)"
  "$PYTHON" -m playwright install chromium || \
    echo "    Neizdevās. Ja Chromium jau ir, norādi PERIODIKA_CHROMIUM=/ceļš/uz/chrome"
fi

echo "==> Sistēmas rīki (neobligāti)"
for tool in tesseract pdftoppm; do
  if command -v "$tool" >/dev/null 2>&1; then
    echo "    [ok]  $tool"
  else
    echo "    [--]  $tool nav atrasts"
  fi
done
if ! command -v tesseract >/dev/null 2>&1; then
  case "$(uname -s)" in
    Darwin) echo "          brew install tesseract tesseract-lang" ;;
    *)      echo "          sudo apt install tesseract-ocr tesseract-ocr-lav tesseract-ocr-frk" ;;
  esac
fi
if ! command -v pdftoppm >/dev/null 2>&1; then
  case "$(uname -s)" in
    Darwin) echo "          brew install poppler" ;;
    *)      echo "          sudo apt install poppler-utils" ;;
  esac
fi

echo
"$PYTHON" -m periodika doctor --offline || true

cat <<NEXT

==> Tālāk:
    $([ "$USE_VENV" -eq 1 ] && echo "source .venv/bin/activate" || true)
    periodika doctor                  # arī tīkla pārbaude
    periodika probe --sample-issue <laidiena-ID>
    periodika mcp-install --target claude-code-lietotāja --write
NEXT
