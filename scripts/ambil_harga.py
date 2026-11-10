"""
Mengambil harga harian PIHPS (Bank Indonesia) dan menyimpannya ke tabel
harga_harian milik backend, dengan sumber = 'PIHPS'.

Cara pakai (dari folder utama repo):
  python -m scripts.ambil_harga
    -> tiap kota mulai dari data terakhirnya sendiri; kota yang belum punya
       data diambil sejak AWAL_DATA (pertama kali untuk semua kota: beberapa jam)
  python -m scripts.ambil_harga --mulai 2025-01-01
    -> mengambil ulang semua kota dari tanggal itu
  python -m scripts.ambil_harga --perbarui-kota
    -> mencocokkan ulang kota PIHPS dengan tabel wilayah

Kota PIHPS dicocokkan otomatis dengan tabel wilayah berdasarkan nama, dan hasilnya
disimpan di ml.kota_pihps. Pencocokan diulang sendiri kalau ada wilayah baru.

Baris dengan kunci yang sama (komoditas, wilayah, jenis pasar, tanggal) ditimpa,
termasuk baris dummy. Harga yang dipakai adalah baris kategori di PIHPS, yaitu
rata-rata semua jenis di bawahnya (misalnya Cabai Merah = rata-rata Besar dan Keriting).

Butuh: pip install -r requirements.txt
"""

import argparse
import os
import re
import ssl
import time
from datetime import date, datetime, timedelta

import psycopg
import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter

AWAL_DATA = date(2024, 1, 1)
# PIHPS kadang merevisi harga beberapa hari terakhir, jadi selalu diambil ulang.
HARI_TARIK_ULANG = 7
# Rentang lebih panjang membuat server PIHPS lambat sampai timeout.
POTONGAN_HARI = 90
JEDA_DETIK = 2  # jeda antar-request supaya tidak membebani server BI
# Data ditulis per kelompok sebesar ini, supaya proses panjang yang terputus
# tidak mengulang dari awal. Run harian biasanya cukup sekali tulis.
BATAS_TULIS = 20000
SUMBER = "PIHPS"

BASE = "https://www.bi.go.id/hargapangan"
HALAMAN = f"{BASE}/TabelHarga/PasarTradisionalDaerah"
LAPORAN_HARIAN = 1
JENIS_PASAR = {"tradisional": 1, "modern": 2}  # nilai price_type_id di PIHPS

# Nama kategori di PIHPS -> (id kategori PIHPS, label di tabel komoditas).
# Tepung terigu tidak ada di PIHPS.
KOMODITAS = {
    "Beras": ("cat_1", "beras"),
    "Telur Ayam": ("cat_4", "telur_ayam"),
    "Bawang Merah": ("cat_5", "bawang_merah"),
    "Bawang Putih": ("cat_6", "bawang_putih"),
    "Cabai Merah": ("cat_7", "cabai_merah"),
    "Minyak Goreng": ("cat_9", "minyak_goreng"),
    "Gula Pasir": ("cat_10", "gula_pasir"),
}

# Kota sampel PIHPS yang namanya berbeda dari kab/kota di tabel wilayah -> kode wilayah.
# Sebagian besar adalah ibukota kabupaten yang dipakai PIHPS sebagai nama kota.
ALIAS_KOTA = {
    "Kota Meulaboh": "11.05",  # Kabupaten Aceh Barat
    "Kota Padang Sidempuan": "12.77",  # ejaan resmi: Padangsidimpuan
    "Kota Tembilahan": "14.04",  # Kabupaten Indragiri Hilir
    "Kota Tanjung Pandan": "19.02",  # Kabupaten Belitung
    "Kota Surakarta (Solo)": "33.72",
    "Kota Singaraja": "51.08",  # Kabupaten Buleleng
    "Kota Maumere": "53.07",  # Kabupaten Sikka
    "Kota Sampit": "62.02",  # Kabupaten Kotawaringin Timur
    "Kota Tanjung": "63.09",  # Kabupaten Tabalong
    "Kota Watampone": "73.08",  # Kabupaten Bone
    "Kota Mamuju": "76.02",  # Kabupaten Mamuju
}


class AdapterTLS(HTTPAdapter):
    """Melonggarkan pilihan enkripsi, tapi sertifikat server tetap diperiksa."""

    def init_poolmanager(self, *args, **kwargs):
        konteks = ssl.create_default_context()
        konteks.set_ciphers("DEFAULT:@SECLEVEL=1")
        kwargs["ssl_context"] = konteks
        return super().init_poolmanager(*args, **kwargs)


def get_dengan_retry(session, url, **kwargs):
    """Coba maksimal 3 kali, dengan jeda makin lama kalau koneksi gagal."""
    for percobaan in range(3):
        try:
            respons = session.get(url, timeout=120, **kwargs)
            respons.raise_for_status()
            return respons
        except (requests.ConnectionError, requests.Timeout):
            if percobaan == 2:
                raise
            tunggu = 60 * (percobaan + 1)
            print(f"Koneksi gagal, coba lagi {tunggu} detik lagi...")
            time.sleep(tunggu)


def buat_session():
    """Buka halaman dulu untuk dapat cookie dan token, seperti yang dilakukan browser."""
    session = requests.Session()
    session.mount("https://", AdapterTLS())
    session.headers["User-Agent"] = "Mozilla/5.0 (tugas kuliah; ambil_harga)"
    respons = get_dengan_retry(session, HALAMAN)
    # Token ini berubah-ubah, jadi selalu diambil dari halaman dan tidak di-hardcode.
    cocok = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', respons.text)
    if cocok:
        session.headers["XSRF-TOKEN"] = cocok.group(1)
    return session


def ambil_pihps(session, endpoint, **params):
    time.sleep(JEDA_DETIK)
    hasil = get_dengan_retry(
        session, f"{BASE}/WebSite/TabelHarga/{endpoint}", params=params
    ).json()
    return hasil["data"] if isinstance(hasil, dict) else hasil


def normalkan(nama):
    """'Kab. Cirebon' dan 'Kabupaten Cirebon' menjadi sama; begitu juga 'Bau-Bau' dan 'Bau Bau'."""
    nama = nama.lower().replace("kab.", "kabupaten").replace("administrasi", "")
    return re.sub(r"[^a-z]", "", nama)


def cocokkan_kota(session, wilayah):
    """Kembalikan baris (wilayah_id, jenis_pasar, provinsi_id, kota_id) untuk semua wilayah.

    provinsi_id dan kota_id bernilai None kalau wilayah itu tidak ada di PIHPS.
    """
    per_kode = {kode: id_wilayah for id_wilayah, kode, _ in wilayah}
    per_nama = {normalkan(nama): id_wilayah for id_wilayah, _, nama in wilayah}
    cocok = {}
    for jenis_pasar, kode_pasar in JENIS_PASAR.items():
        for provinsi in ambil_pihps(session, "GetRefProvince", price_type_id=kode_pasar):
            daftar_kota = ambil_pihps(
                session, "GetRefRegency", price_type_id=kode_pasar, ref_prov_id=provinsi["id"]
            )
            for kota in daftar_kota:
                nama = str(kota["name"]).strip()
                id_wilayah = per_kode.get(ALIAS_KOTA.get(nama)) or per_nama.get(normalkan(nama))
                if id_wilayah:
                    cocok[(id_wilayah, jenis_pasar)] = (provinsi["id"], kota["id"])
    return [
        (id_wilayah, jenis_pasar, *cocok.get((id_wilayah, jenis_pasar), (None, None)))
        for id_wilayah, _, _ in wilayah
        for jenis_pasar in JENIS_PASAR
    ]


def potong_rentang(mulai, selesai):
    while mulai <= selesai:
        akhir = min(mulai + timedelta(days=POTONGAN_HARI - 1), selesai)
        yield mulai, akhir
        mulai = akhir + timedelta(days=1)


def ambil_grid(session, jenis_pasar, provinsi, kota, mulai, selesai):
    """Satu request untuk semua komoditas sekaligus di satu kota dan jenis pasar."""
    return ambil_pihps(
        session,
        "GetGridDataDaerah",
        price_type_id=JENIS_PASAR[jenis_pasar],
        comcat_id=",".join(kategori for kategori, _ in KOMODITAS.values()),
        province_id=provinsi,
        regency_id=kota,
        market_id="",
        tipe_laporan=LAPORAN_HARIAN,
        start_date=mulai.isoformat(),
        end_date=selesai.isoformat(),
    )


def ubah_ke_baris(grid):
    """PIHPS mengirim satu kolom per tanggal; diubah menjadi (tanggal, label, harga)."""
    for item in grid:
        if item.get("level") != 1:
            continue  # level 2 adalah jenis turunan, mis. Cabai Merah Keriting
        nama = str(item["name"]).strip()
        if nama not in KOMODITAS:
            print(f"Peringatan: kategori PIHPS tidak dikenal dilewati: {nama}")
            continue
        label = KOMODITAS[nama][1]
        for kolom, nilai in item.items():
            if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", kolom):
                continue
            angka = re.sub(r"[^\d]", "", str(nilai))  # "61,900" -> "61900"; "-" -> ""
            if angka:
                yield datetime.strptime(kolom, "%d/%m/%Y").date(), label, int(angka)


def sambung():
    # Format DATABASE_URL mengikuti backend, tapi driver hanya mengenal postgresql://.
    url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    # prepare_threshold=None: prepared statement tidak cocok dengan connection pooler.
    return psycopg.connect(url, prepare_threshold=None)


def baca_status(kursor):
    """Semua nama tabel ditulis lengkap dengan schema supaya tidak bergantung pada search_path."""
    kursor.execute("""
        CREATE SCHEMA IF NOT EXISTS ml;
        CREATE TABLE IF NOT EXISTS ml.kota_pihps (
            wilayah_id INTEGER NOT NULL,
            jenis_pasar TEXT NOT NULL,
            provinsi_id INTEGER,  -- NULL berarti wilayah ini tidak ada di PIHPS
            kota_id INTEGER,
            -- Tanggal terakhir yang sudah dicek, walaupun PIHPS tidak punya datanya.
            -- Tanpa ini kota yang datanya kosong akan diambil ulang dari awal tiap hari.
            diambil_sampai DATE,
            PRIMARY KEY (wilayah_id, jenis_pasar)
        );
    """)
    label = [label for _, label in KOMODITAS.values()]
    kursor.execute("SELECT label, id FROM public.komoditas WHERE label = ANY(%s)", [label])
    id_komoditas = dict(kursor.fetchall())
    hilang = sorted(set(label) - set(id_komoditas))
    if hilang:
        raise RuntimeError(f"Belum ada di tabel komoditas backend: {hilang}")

    kursor.execute("SELECT id, kode, nama FROM public.wilayah ORDER BY kode")
    wilayah = kursor.fetchall()
    kursor.execute(
        "SELECT wilayah_id, jenis_pasar, provinsi_id, kota_id, diambil_sampai FROM ml.kota_pihps"
    )
    kota = kursor.fetchall()
    kursor.execute(
        "SELECT wilayah_id, jenis_pasar, max(tanggal) FROM public.harga_harian "
        "WHERE sumber = %s GROUP BY 1, 2",
        [SUMBER],
    )
    terakhir = {(w, j): t for w, j, t in kursor.fetchall()}
    return id_komoditas, wilayah, kota, terakhir


def simpan_kota(kota):
    """Perbarui pemetaan tanpa menghapus catatan diambil_sampai yang sudah ada."""
    with sambung() as koneksi, koneksi.cursor() as kursor:
        kursor.execute(
            "DELETE FROM ml.kota_pihps WHERE wilayah_id <> ALL(%s)",
            [sorted({id_wilayah for id_wilayah, *_ in kota})],
        )
        kursor.executemany(
            "INSERT INTO ml.kota_pihps (wilayah_id, jenis_pasar, provinsi_id, kota_id) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (wilayah_id, jenis_pasar) DO UPDATE "
            "SET provinsi_id = EXCLUDED.provinsi_id, kota_id = EXCLUDED.kota_id",
            kota,
        )
        kursor.execute(
            "SELECT wilayah_id, jenis_pasar, provinsi_id, kota_id, diambil_sampai FROM ml.kota_pihps"
        )
        return kursor.fetchall()


def simpan(data, pasangan_selesai, selesai):
    """Tulis harga dan catat kota yang sudah dicek, dalam satu transaksi.

    Semua harga ditulis dengan satu perintah; baris yang harganya tidak berubah dilewati.
    """
    if not pasangan_selesai:
        return 0
    with sambung() as koneksi, koneksi.cursor() as kursor:
        id_wilayah, jenis_pasar = zip(*pasangan_selesai)
        kursor.execute(
            """
            UPDATE ml.kota_pihps k
            SET diambil_sampai = GREATEST(k.diambil_sampai, %s)
            FROM unnest(%s::int[], %s::text[]) AS u(wilayah_id, jenis_pasar)
            WHERE k.wilayah_id = u.wilayah_id AND k.jenis_pasar = u.jenis_pasar
            """,
            [selesai, list(id_wilayah), list(jenis_pasar)],
        )
        if not data:
            return 0
        kolom = list(zip(*[(*kunci, harga) for kunci, harga in data.items()]))
        kursor.execute(
            """
            INSERT INTO public.harga_harian AS h
                (tanggal, komoditas_id, wilayah_id, jenis_pasar, harga, sumber)
            SELECT u.tanggal, u.komoditas_id, u.wilayah_id, u.jenis_pasar, u.harga, %s
            FROM unnest(%s::date[], %s::int[], %s::int[], %s::text[], %s::int[])
                AS u(tanggal, komoditas_id, wilayah_id, jenis_pasar, harga)
            ON CONFLICT (komoditas_id, wilayah_id, jenis_pasar, tanggal) DO UPDATE
            SET harga = EXCLUDED.harga, sumber = EXCLUDED.sumber
            WHERE (h.harga, h.sumber) IS DISTINCT FROM (EXCLUDED.harga, EXCLUDED.sumber)
            """,
            [SUMBER, *[list(isi) for isi in kolom]],
        )
        return kursor.rowcount


def main():
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--mulai", type=date.fromisoformat)
    parser.add_argument("--selesai", type=date.fromisoformat, default=date.today())
    parser.add_argument("--perbarui-kota", action="store_true")
    args = parser.parse_args()

    # Koneksi hanya dibuka saat perlu, tidak selama scraping, supaya compute Neon cepat tidur.
    with sambung() as koneksi, koneksi.cursor() as kursor:
        id_komoditas, wilayah, kota, terakhir = baca_status(kursor)

    session = buat_session()
    sudah_dicek = {id_wilayah for id_wilayah, *_ in kota}
    if args.perbarui_kota or any(id_wilayah not in sudah_dicek for id_wilayah, _, _ in wilayah):
        print("Mencocokkan kota PIHPS dengan tabel wilayah...")
        kota = simpan_kota(cocokkan_kota(session, wilayah))
    kode_wilayah = {id_wilayah: kode for id_wilayah, kode, _ in wilayah}
    pasangan = sorted(
        (kode_wilayah[w], w, j, p, k, sampai)
        for w, j, p, k, sampai in kota
        if k is not None and w in kode_wilayah
    )
    print(f"{len(pasangan)} pasangan kota-jenis pasar, sampai {args.selesai}")

    data, pasangan_selesai, jumlah_diambil, jumlah_ditulis = {}, [], 0, 0
    for kode, id_wilayah, jenis_pasar, provinsi, kota_pihps, sampai in pasangan:
        # Harga terakhir di harga_harian dipakai untuk data yang masuk sebelum
        # kolom diambil_sampai ada.
        sampai = sampai or terakhir.get((id_wilayah, jenis_pasar))
        if args.mulai:
            mulai = args.mulai
        elif sampai:
            mulai = sampai - timedelta(days=HARI_TARIK_ULANG)
        else:
            mulai = AWAL_DATA
        jumlah = 0
        for awal, akhir in potong_rentang(mulai, args.selesai):
            grid = ambil_grid(session, jenis_pasar, provinsi, kota_pihps, awal, akhir)
            for tanggal, label, harga in ubah_ke_baris(grid):
                data[(tanggal, id_komoditas[label], id_wilayah, jenis_pasar)] = harga
                jumlah += 1
        print(f"{kode} {jenis_pasar}: {jumlah} harga sejak {mulai}")
        jumlah_diambil += jumlah
        pasangan_selesai.append((id_wilayah, jenis_pasar))
        if len(data) >= BATAS_TULIS:
            jumlah_ditulis += simpan(data, pasangan_selesai, args.selesai)
            data, pasangan_selesai = {}, []
    jumlah_ditulis += simpan(data, pasangan_selesai, args.selesai)
    print(f"Selesai. {jumlah_diambil} harga diambil, {jumlah_ditulis} baris baru/berubah ditulis.")


if __name__ == "__main__":
    main()
