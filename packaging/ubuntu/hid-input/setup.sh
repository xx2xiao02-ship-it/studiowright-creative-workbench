#!/usr/bin/env bash
set -euo pipefail

if [[ $(id -u) != 0 ]]; then
    echo '需要管理员权限来配置 /dev/uinput。' >&2
    exit 1
fi

rule='KERNEL=="uinput", SUBSYSTEM=="misc", MODE="0660", TAG+="uaccess"'
rules_file=/etc/udev/rules.d/60-studiowright-uinput.rules
module_file=/etc/modules-load.d/studiowright-uinput.conf

for path in "$rules_file" "$module_file"; do
    if [[ -L $path || ( -e $path && ! -f $path ) ]]; then
        echo "无法安全使用系统配置路径：$path" >&2
        exit 1
    fi
done
if [[ ( -f $rules_file && $(cat "$rules_file") != "$rule" ) ||
      ( -f $module_file && $(cat "$module_file") != uinput ) ]]; then
    echo '已存在自定义 StudioWright 输入配置，请管理员先核对现有规则。' >&2
    exit 1
fi

if ! grep -Eq '[[:space:]]uinput$' /proc/misc; then
    modprobe uinput
fi
if [[ ! -c /dev/uinput ]]; then
    udevadm settle --timeout=10
fi
[[ -c /dev/uinput ]] || { echo '系统未创建 /dev/uinput。' >&2; exit 1; }

printf '%s\n' "$rule" | install -m 0644 /dev/stdin "$rules_file"
printf '%s\n' uinput | install -m 0644 /dev/stdin "$module_file"
udevadm control --reload-rules
udevadm trigger --action=add --subsystem-match=misc --sysname-match=uinput
udevadm settle --timeout=10
