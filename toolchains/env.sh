# Source this file from bash to use the workspace-local Miosix compiler.
TYT_TOOLCHAIN_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PATH="$TYT_TOOLCHAIN_DIR/arm-miosix-eabi/bin:$PATH"
unset TYT_TOOLCHAIN_DIR
