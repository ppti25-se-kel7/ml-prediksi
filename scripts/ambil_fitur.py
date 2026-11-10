"""
Mengumpulkan fitur pendukung harga pangan dan menyimpannya ke PostgreSQL (Neon).

Data disimpan di schema ml (tabel kalender, cuaca, kurs, harga_dunia). Fitur tiap
komoditas dibaca lewat view, misalnya: SELECT * FROM ml.fitur_cabai_merah;
Nama komoditas mengikuti kolom label di tabel komoditas milik backend.

Cara pakai (dari folder utama repo):
  python -m scripts.ambil_fitur
    -> kalau database kosong, mengambil semua data sejak AWAL_DATA;
       kalau sudah ada isinya, hanya mengambil data terbaru
  python -m scripts.ambil_fitur --mulai 2024-01-01
    -> mengambil ulang dari tanggal itu dan membuat ulang view
       (perlu setelah mengubah LOKASI, HARGA_DUNIA, atau KOMODITAS)

Lokasi sentra produksi adalah asumsi awal, silakan diganti sesuai pemasok.

Koneksi dibaca dari DATABASE_URL di file .env, formatnya sama dengan backend:
  DATABASE_URL=postgresql+psycopg://user:password@host.neon.tech/dbname?sslmode=require

Butuh: pip install -r requirements.txt
"""

import argparse
import os
import re
import time
from datetime import date, timedelta
from io import BytesIO

import holidays
import pandas as pd
import psycopg
import requests
from dotenv import load_dotenv

AWAL_DATA = date(2024, 1, 1)
# Beberapa hari terakhir selalu diambil ulang: nilai cuaca terbaru masih bisa
# direvisi sumbernya, dan hari saat laptop mati ikut terisi pada run berikutnya.
HARI_TARIK_ULANG = 7
JEDA_DETIK = 1  # jeda antar-request supaya tidak membebani server sumber
HEADER = {"User-Agent": "tugas-kuliah-fitur-harga/1.0"}

URL_CUACA = "https://archive-api.open-meteo.com/v1/archive"
URL_KURS = "https://api.frankfurter.dev/v1"
HALAMAN_PINK_SHEET = "https://www.worldbank.org/en/research/commodity-markets"

LOKASI = {
    "cianjur": (-6.82, 107.14),
    "brebes": (-6.87, 109.05),
    "karawang": (-6.30, 107.30),
    "blitar": (-8.10, 112.16),
}

# Nama fitur -> nama variabel harian di Open-Meteo.
VARIABEL_CUACA = {
    "suhu": "temperature_2m_mean",
    "rh": "relative_humidity_2m_mean",
    "hujan": "precipitation_sum",
    "radiasi": "shortwave_radiation_sum",
}

# Nama kolom di World Bank Pink Sheet -> nama fitur.
HARGA_DUNIA = {
    "Palm oil": "sawit_usd_ton",
    "Maize": "jagung_usd_ton",
    "Soybean meal": "bungkil_kedelai_usd_ton",
    "Rice, Thai 5%": "beras_thai_usd_ton",
    "Sugar, world": "gula_usd_kg",
    "Wheat, US HRW": "gandum_usd_ton",
}

TANAMAN = ["hujan", "rh", "suhu", "radiasi"]  # hasil panen peka terhadap cuaca
TERNAK = ["suhu", "rh"]  # panas dan lembap menurunkan produktivitas unggas

KOMODITAS = {
    "beras": {
        "cuaca": {"karawang": TANAMAN},
        "dunia": ["beras_thai_usd_ton"],
        "kurs": True,
    },
    "telur_ayam": {
        "cuaca": {"blitar": TERNAK},
        "dunia": ["jagung_usd_ton", "bungkil_kedelai_usd_ton"],  # bahan pakan
        "kurs": True,
    },
    "bawang_merah": {"cuaca": {"brebes": TANAMAN}, "dunia": [], "kurs": False},
    "bawang_putih": {"cuaca": {}, "dunia": [], "kurs": True},  # mayoritas impor
    "cabai_merah": {"cuaca": {"cianjur": TANAMAN}, "dunia": [], "kurs": False},
    "minyak_goreng": {"cuaca": {}, "dunia": ["sawit_usd_ton"], "kurs": True},
    "gula_pasir": {"cuaca": {}, "dunia": ["gula_usd_kg"], "kurs": True},
    "tepung_terigu": {"cuaca": {}, "dunia": ["gandum_usd_ton"], "kurs": True},  # gandum impor
}


def get(url, **params):
    time.sleep(JEDA_DETIK)
    respons = requests.get(url, params=params, headers=HEADER, timeout=120)
    respons.raise_for_status()
    return respons


def ambil_cuaca(lokasi, mulai, selesai):
    lintang, bujur = LOKASI[lokasi]
    hasil = get(
        URL_CUACA,
        latitude=lintang,
        longitude=bujur,
        start_date=mulai.isoformat(),
        end_date=selesai.isoformat(),
        daily=",".join(VARIABEL_CUACA.values()),
        timezone="Asia/Jakarta",
    ).json()["daily"]
    tabel = pd.DataFrame(hasil)
    tabel["time"] = pd.to_datetime(tabel["time"])
    tabel = tabel.set_index("time")
    return tabel.rename(columns={v: k for k, v in VARIABEL_CUACA.items()})


def ambil_kurs(hari):
    # Mulai lebih awal agar hari pertama tetap terisi kalau jatuh di hari libur bursa.
    mulai = hari[0].date() - timedelta(days=10)
    selesai = hari[-1].date()
    kurs = get(
        f"{URL_KURS}/{mulai.isoformat()}..{selesai.isoformat()}",
        base="USD",
        symbols="IDR",
    ).json()["rates"]
    deret = pd.Series({pd.Timestamp(t): nilai["IDR"] for t, nilai in kurs.items()})
    # Kurs tidak terbit di akhir pekan, jadi memakai nilai hari kerja terakhir.
    return deret.reindex(deret.index.union(hari)).sort_index().ffill().reindex(hari)


def ambil_harga_dunia(mulai):
    # Alamat file berubah tiap rilis, jadi selalu dicari dari halaman resminya.
    halaman = get(HALAMAN_PINK_SHEET).text
    cocok = re.search(r'https?://[^"\']*CMO-Historical-Data-Monthly\.xlsx', halaman)
    if not cocok:
        raise RuntimeError("Tautan Pink Sheet tidak ditemukan di halaman World Bank.")

    tabel = pd.read_excel(
        BytesIO(get(cocok.group()).content),
        sheet_name="Monthly Prices",
        header=4,
        skiprows=[5],  # baris satuan
    )
    tabel.columns = [str(kolom).strip() for kolom in tabel.columns]
    tabel = tabel.rename(columns={tabel.columns[0]: "bulan", **HARGA_DUNIA})
    tabel["bulan"] = pd.PeriodIndex(
        tabel["bulan"].str.replace("M", "-"), freq="M"
    )
    tabel = tabel.set_index("bulan")[list(HARGA_DUNIA.values())]
    tabel = tabel.apply(pd.to_numeric, errors="coerce")
    # Satu bulan sebelum mulai ikut disimpan karena dipakai view pada bulan pertama.
    tabel = tabel[tabel.index >= pd.Period(mulai, "M") - 1]
    tabel.index = tabel.index.to_timestamp()
    return tabel


def buat_kalender_libur(hari):
    # Tahun berikutnya ikut diambil untuk menghitung hitung mundur di akhir periode.
    tahun = range(hari[0].year, hari[-1].year + 2)
    libur = holidays.Indonesia(years=tahun, language="id")

    lebaran = {}
    for tanggal, nama in libur.items():
        if "Idul Fitri" in nama and "kedua" not in nama and "Cuti" not in nama:
            lebaran[tanggal.year] = min(tanggal, lebaran.get(tanggal.year, tanggal))
    daftar_lebaran = pd.DatetimeIndex(sorted(lebaran.values()))

    posisi = daftar_lebaran.searchsorted(hari)
    return pd.DataFrame(
        {
            "libur": [int(t.date() in libur) for t in hari],
            "hari_ke_lebaran": (daftar_lebaran[posisi] - hari).days,
        },
        index=hari,
    )


def sambung():
    # Format DATABASE_URL mengikuti backend, tapi driver hanya mengenal postgresql://.
    url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    # prepare_threshold=None: prepared statement tidak cocok dengan connection pooler.
    return psycopg.connect(url, prepare_threshold=None)


def baca_status(kursor):
    """Kembalikan (tanggal terakhir, bulan harga dunia terakhir) yang sudah tersimpan."""
    kursor.execute("SELECT to_regclass('ml.cuaca')")
    if kursor.fetchone()[0] is None:
        return None, None
    kursor.execute(
        "SELECT (SELECT max(tanggal) FROM ml.cuaca), (SELECT max(bulan) FROM ml.harga_dunia)"
    )
    return kursor.fetchone()


def buat_tabel(kursor):
    # Dipisah di schema ml karena schema public dikelola Alembic backend: tabel di
    # public yang tidak ada di model backend akan diusulkan untuk dihapus.
    kolom_dunia = ", ".join(f"{nama} DOUBLE PRECISION" for nama in HARGA_DUNIA.values())
    kursor.execute(f"""
        CREATE SCHEMA IF NOT EXISTS ml;
        CREATE TABLE IF NOT EXISTS ml.kalender (
            tanggal DATE PRIMARY KEY,
            libur SMALLINT NOT NULL,
            hari_ke_lebaran INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ml.cuaca (
            tanggal DATE NOT NULL,
            lokasi TEXT NOT NULL,
            suhu DOUBLE PRECISION,
            rh DOUBLE PRECISION,
            hujan DOUBLE PRECISION,
            radiasi DOUBLE PRECISION,
            PRIMARY KEY (tanggal, lokasi)
        );
        CREATE TABLE IF NOT EXISTS ml.kurs (
            tanggal DATE PRIMARY KEY,
            usd_idr DOUBLE PRECISION
        );
        CREATE TABLE IF NOT EXISTS ml.harga_dunia (
            bulan DATE PRIMARY KEY,
            {kolom_dunia}
        );
    """)


def simpan(kursor, nama_tabel, tabel, kunci):
    """Tulis semua baris dalam satu perintah, dan lewati baris yang nilainya tidak berubah."""
    if tabel.empty:
        return 0
    tabel = tabel.reset_index()
    tabel[kunci[0]] = tabel[kunci[0]].dt.date
    kolom = list(tabel.columns)
    ubah = [k for k in kolom if k not in kunci]
    baris = tabel.astype(object).where(tabel.notna(), None).values.tolist()

    satu_baris = "(" + ", ".join(["%s"] * len(kolom)) + ")"
    perintah = (
        f"INSERT INTO ml.{nama_tabel} AS t ({', '.join(kolom)}) "
        f"VALUES {', '.join([satu_baris] * len(baris))} "
        f"ON CONFLICT ({', '.join(kunci)}) DO UPDATE "
        f"SET {', '.join(f'{k} = EXCLUDED.{k}' for k in ubah)} "
        f"WHERE ({', '.join(f't.{k}' for k in ubah)}) "
        f"IS DISTINCT FROM ({', '.join(f'EXCLUDED.{k}' for k in ubah)})"
    )
    kursor.execute(perintah, [nilai for isi in baris for nilai in isi])
    return kursor.rowcount


def buat_view(kursor):
    """Satu view per komoditas, berisi hanya fitur yang relevan untuk komoditas itu.

    Harga dunia bulan M baru terbit awal bulan M+1, jadi pada bulan M yang sudah
    diketahui adalah harga bulan M-1. Tanpa pergeseran ini model "mengintip" masa depan.
    """
    for nama, aturan in KOMODITAS.items():
        kolom = ["k.tanggal", "k.libur", "k.hari_ke_lebaran"]
        gabung = []
        for i, (lokasi, variabel) in enumerate(aturan["cuaca"].items()):
            kolom += [f"c{i}.{v} AS {v}_{lokasi}" for v in variabel]
            gabung.append(
                f"LEFT JOIN ml.cuaca c{i} ON c{i}.tanggal = k.tanggal AND c{i}.lokasi = '{lokasi}'"
            )
        if aturan["kurs"]:
            kolom.append("ku.usd_idr AS kurs_usd_idr")
            gabung.append("LEFT JOIN ml.kurs ku ON ku.tanggal = k.tanggal")
        if aturan["dunia"]:
            kolom += [f"d.{d} AS {d}_bln_lalu" for d in aturan["dunia"]]
            gabung.append(
                "LEFT JOIN ml.harga_dunia d "
                "ON d.bulan = (date_trunc('month', k.tanggal) - INTERVAL '1 month')::date"
            )
        # DROP dulu karena CREATE OR REPLACE gagal kalau susunan kolomnya berubah.
        kursor.execute(f"DROP VIEW IF EXISTS ml.fitur_{nama}")
        kursor.execute(
            f"CREATE VIEW ml.fitur_{nama} AS SELECT {', '.join(kolom)} "
            f"FROM ml.kalender k {' '.join(gabung)}"
        )


def main():
    load_dotenv()
    hari_ini = date.today()
    parser = argparse.ArgumentParser()
    parser.add_argument("--mulai", type=date.fromisoformat)
    parser.add_argument("--selesai", type=date.fromisoformat, default=hari_ini - timedelta(days=1))
    args = parser.parse_args()

    # Koneksi hanya dibuka saat perlu, tidak selama scraping, supaya compute Neon
    # (kuota paket gratis) cepat tidur lagi.
    with sambung() as koneksi, koneksi.cursor() as kursor:
        tanggal_terakhir, bulan_terakhir = baca_status(kursor)

    pertama_kali = tanggal_terakhir is None
    if args.mulai:
        mulai = args.mulai
    elif pertama_kali:
        mulai = AWAL_DATA
    else:
        mulai = tanggal_terakhir - timedelta(days=HARI_TARIK_ULANG)
    hari = pd.date_range(mulai, args.selesai, freq="D")
    print(f"Periode: {mulai} s.d. {args.selesai}")

    lokasi_dipakai = sorted({l for a in KOMODITAS.values() for l in a["cuaca"]})
    bagian_cuaca = []
    for lokasi in lokasi_dipakai:
        print(f"Mengambil cuaca {lokasi}...")
        tabel = ambil_cuaca(lokasi, mulai, args.selesai)
        bagian_cuaca.append(tabel.rename_axis("tanggal").assign(lokasi=lokasi))
    cuaca = pd.concat(bagian_cuaca)

    print("Mengambil kurs...")
    kurs = ambil_kurs(hari).rename("usd_idr").rename_axis("tanggal").to_frame()

    # File harga dunia hanya berubah sebulan sekali, jadi tidak perlu diunduh tiap hari.
    bulan_lalu = (hari_ini.replace(day=1) - timedelta(days=1)).replace(day=1)
    dunia = pd.DataFrame()
    if args.mulai or bulan_terakhir is None or bulan_terakhir < bulan_lalu:
        print("Mengambil harga dunia...")
        dunia = ambil_harga_dunia(mulai).rename_axis("bulan")

    kalender = buat_kalender_libur(hari).rename_axis("tanggal")

    # Satu transaksi: kalau ada yang gagal tidak ada data setengah jadi,
    # dan run berikutnya mengulang dari tanggal yang sama.
    with sambung() as koneksi, koneksi.cursor() as kursor:
        struktur_baru = pertama_kali or bool(args.mulai)
        if struktur_baru:
            buat_tabel(kursor)
        berubah = {
            "cuaca": simpan(kursor, "cuaca", cuaca, ["tanggal", "lokasi"]),
            "kurs": simpan(kursor, "kurs", kurs, ["tanggal"]),
            "harga_dunia": simpan(kursor, "harga_dunia", dunia, ["bulan"]),
            "kalender": simpan(kursor, "kalender", kalender, ["tanggal"]),
        }
        if struktur_baru:
            buat_view(kursor)
    print(f"Selesai. Baris ditulis: {berubah}")


if __name__ == "__main__":
    main()
