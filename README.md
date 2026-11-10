# ml-prediksi

Pengambilan data harian untuk model prediksi harga pangan SEPI. Datanya ditulis ke database Neon yang sama dengan `sepi-backend`.

| Script | Sumber | Ditulis ke |
| --- | --- | --- |
| `scripts/ambil_harga.py` | PIHPS Bank Indonesia | `public.harga_harian` (`sumber = 'PIHPS'`) dan `ml.kota_pihps` |
| `scripts/ambil_fitur.py` | Open-Meteo, Frankfurter, World Bank Pink Sheet, kalender libur | schema `ml`; fitur per komoditas dibaca lewat view `ml.fitur_<label>` |

Tabel `komoditas` dan `wilayah` milik backend harus sudah terisi, karena kota PIHPS dicocokkan otomatis dengan tabel `wilayah`.

## Menjalankan di lokal

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Isi `DATABASE_URL` di `.env` (sama dengan backend), lalu:

```
python -m scripts.ambil_fitur
python -m scripts.ambil_harga
```

## Menjalankan di VPS

Butuh Python 3.10 ke atas. Di Ubuntu/Debian:

```
sudo apt install -y python3-venv git
git clone https://github.com/ppti25-se-kel7/ml-prediksi.git
cd ml-prediksi
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
nano .env            # isi DATABASE_URL
chmod 600 .env
```

Cek dulu VPS bisa mengakses PIHPS. Sebagian situs pemerintah memblokir IP luar negeri atau IP datacenter:

```
curl -sS -o /dev/null -w "%{http_code}\n" https://www.bi.go.id/hargapangan/TabelHarga/PasarTradisionalDaerah
```

Hasil `200` berarti aman.

Pengisian awal untuk semua kota butuh beberapa jam. Jalankan di latar belakang supaya tetap berjalan setelah SSH ditutup:

```
nohup scripts/jalankan_harian.sh &
tail -f harian.log
```

Lalu jadwalkan tiap hari dengan `crontab -e`. Jam cron mengikuti zona waktu VPS, cek dengan `timedatectl`:

```
# VPS memakai WIB (Asia/Jakarta): jam 08.00
0 8 * * * /home/USER/ml-prediksi/scripts/jalankan_harian.sh

# VPS memakai UTC: 01.00 UTC = 08.00 WIB
0 1 * * * /home/USER/ml-prediksi/scripts/jalankan_harian.sh
```

Ganti `/home/USER/ml-prediksi` dengan hasil `pwd` di folder repo. Kalau run sebelumnya masih berjalan, run baru otomatis dilewati.

Memperbarui kode di VPS:

```
git pull
.venv/bin/pip install -r requirements.txt
```

## Hemat kuota Neon

- Koneksi hanya dibuka saat membaca dan menulis, tidak selama scraping, supaya compute Neon cepat tidur.
- Baris yang nilainya tidak berubah tidak ditulis ulang.
- Jangan menjalankan `pg_dump` lewat host `-pooler`. Pakai host tanpa `-pooler` kalau perlu backup.

## Catatan data

- Tepung terigu tidak ada di PIHPS.
- PIHPS mencatat minyak goreng per kg, sedangkan tabel `komoditas` memakai liter.
- Beberapa wilayah tidak punya data PIHPS, misalnya Kepulauan Seribu, Jakarta selain Jakarta Pusat, Kab. Bogor, Kab. Bekasi, Kab. Tangerang, dan Tangerang Selatan.
- Lokasi cuaca per komoditas di `scripts/ambil_fitur.py` (`LOKASI`) adalah asumsi sentra produksi dan bisa diganti.
