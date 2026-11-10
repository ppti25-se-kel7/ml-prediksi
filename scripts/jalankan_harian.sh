#!/usr/bin/env bash
# Dipanggil cron tiap hari. Hasil dan error dicatat di harian.log di folder utama repo.
# Kedua script berjalan terpisah: kalau satu gagal, yang lain tetap jalan.
set -u
cd "$(dirname "$0")/.."

# Tanggal "hari ini" mengikuti WIB walaupun jam VPS memakai UTC.
export TZ=Asia/Jakarta
# Supaya progres langsung muncul di log, tidak menunggu buffer penuh.
export PYTHONUNBUFFERED=1

# Pengisian awal bisa makan beberapa jam. Kalau cron jalan saat itu masih berlangsung,
# run baru dilewati supaya dua proses tidak menulis bersamaan.
# flock selalu ada di Linux; kalau tidak ada (mis. Git Bash), jalan tanpa kunci.
exec 9>.harian.lock
if command -v flock >/dev/null && ! flock -n 9; then
  echo "$(date '+%F %T') dilewati: run sebelumnya masih berjalan" >> harian.log
  exit 0
fi

{
  echo "===== $(date '+%F %T')"
  .venv/bin/python -m scripts.ambil_fitur
  .venv/bin/python -m scripts.ambil_harga
} >> harian.log 2>&1
