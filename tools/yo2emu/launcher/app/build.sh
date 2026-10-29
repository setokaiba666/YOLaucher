#!/bin/sh
# Compila el lanzador de macOS: ../dist-app/YO2OnlineLauncher.app
# El juego (yo2.exe) se abre con CrossOver. Hace falta Go, el CLI de Wails y,
# para incrustar la DLL de red, zig (brew install go zig).
set -eu
cd "$(dirname "$0")/.."
here="$(pwd)/app"

if ! command -v go >/dev/null 2>&1; then
  echo "Falta Go. brew install go" >&2
  exit 1
fi
if ! command -v wails >/dev/null 2>&1; then
  echo "Instalo wails..."
  go install github.com/wailsapp/wails/v2/cmd/wails@v2.16.0
  export PATH="$(go env GOPATH)/bin:$PATH"
fi
if command -v zig >/dev/null 2>&1; then
  # zig cc (no gcc de MinGW): primero el objeto con las cabeceras de zig, luego el enlace.
  # --entry,DllMain@12: lld añade el _ de stdcall. La literal de la marca se guarda en marca_fichero.
  real=$(realpath "$(command -v zig)")
  inc="$(dirname "$(dirname "$real")")/lib/zig/libc/include/any-windows-any"
  obj="$(mktemp -t yo2net).obj"
  tmp="$(mktemp -t yo2net).dll"
  zig cc -target x86-windows-gnu -c -O2 -ffreestanding -fno-builtin -fno-stack-protector \
    -fno-asynchronous-unwind-tables -Wall -Wno-unused-function -I "$inc" \
    juego/dll/yo2net.c -o "$obj"
  zig cc -target x86-windows-gnu -shared -nostdlib \
    -Wl,--entry,DllMain@12 -Wl,--image-base,0x6a5d0000 -Wl,--dynamicbase -Wl,--nxcompat \
    "$obj" juego/dll/version.def -lkernel32 -luser32 -lgdi32 -o "$tmp"
  rm -f "$obj"
  mv "$tmp" juego/dllbin/yo2net-version.dll
else
  echo "Sin zig: el lanzador no llevará version.dll y no podrá enganchar el juego." >&2
fi

ver="$(date -u +%Y%m%d%H%M%S)"
cd "$here"
# Sin -o: en macOS Wails arma YO2OnlineLauncher.app (un -o con ese nombre deja un binario suelto).
rm -rf build/bin/YO2OnlineLauncher.app
CGO_ENABLED=1 wails build -s -skipbindings -trimpath \
  -ldflags "-s -w -X main.versionLanzador=$ver"
cd ..
mkdir -p dist-app
rm -rf dist-app/YO2OnlineLauncher.app
cp -R app/build/bin/YO2OnlineLauncher.app dist-app/
echo "Hecho: $(pwd)/dist-app/YO2OnlineLauncher.app"
