#!/usr/bin/env bash
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run: sudo ./configure-smb.sh'; exit 1; }
read -r -p 'SMB share (e.g. //nas.example.local/backups): ' SERVER
[[ $SERVER =~ ^//[A-Za-z0-9._-]+/[A-Za-z0-9._$-]+$ ]] || { echo 'Invalid SMB share'; exit 1; }
read -r -p 'Subfolder on share [FieldBackup]: ' REMOTE_DIR
REMOTE_DIR=${REMOTE_DIR:-FieldBackup}
[[ $REMOTE_DIR =~ ^[A-Za-z0-9._-]+$ ]] || { echo 'Invalid subfolder'; exit 1; }
read -r -p 'SMB username: ' USERNAME
[[ -n $USERNAME && $USERNAME != *$'\n'* ]] || exit 1
read -r -s -p 'SMB password: ' PASSWORD; echo
[[ -n $PASSWORD && $PASSWORD != *$'\n'* ]] || exit 1
umask 077
printf 'SERVER=%q\nREMOTE_DIR=%q\n' "$SERVER" "$REMOTE_DIR" > /etc/fieldbackup-smb.conf
printf 'username=%s\npassword=%s\n' "$USERNAME" "$PASSWORD" > /etc/fieldbackup-smb.credentials
unset PASSWORD
chmod 600 /etc/fieldbackup-smb.conf /etc/fieldbackup-smb.credentials
systemctl enable --now fieldbackup-sync.timer
echo "SMB sync timer enabled. Target: $SERVER/$REMOTE_DIR"
echo 'Test manually: sudo systemctl start fieldbackup-sync.service'
