#!/bin/sh
# phpvm 邮件捕获（F11）：PHP mail() 经 sendmail_path 调用本脚本，
# 把邮件原文（头部 + 正文）落盘为 .eml，供 phpvm「邮件」页查看。
# 仅用 POSIX shell，无外部依赖；Windows 不适用（mail() 走 SMTP）。
dir="/Users/mou/www/wnrp/mail"
mkdir -p "$dir" 2>/dev/null
umask 022
f="$dir/$(date +%Y%m%d-%H%M%S)-$$.eml"
cat > "$f"
