# Catatan verifikasi — 17 November 2026

- **20 tes lulus** (`python -m pytest -q`): normalisasi link YouTube/Shorts, penolakan domain lain, pemilihan format dengan audio, prioritas stream langsung, alur API → antrean → berkas, riwayat persisten, kualitas tidak valid/kedaluwarsa, kegagalan unduhan, batas lokasi berkas, progres, dan pemulihan status setelah restart.
- Penggabungan nyata diuji lewat `FFmpegMergerPP` milik yt-dlp dengan video dan audio sintetis satu detik. Berkas hasil berhasil didekode dengan pemetaan eksplisit stream video dan audio.
- `node --check app/static/app.js` dan `pip check` lulus.
- Uji browser: cek link → daftar kualitas 240p/144p → pilih 240p → unduh → selesai. Contoh publik: `https://www.youtube.com/watch?v=jNQXAC9IVRw`.
- Hasil YouTube asli: `Me at the zoo [jNQXAC9IVRw].mp4`, 629.172 byte, video H.264 320×240 dan audio AAC stereo 44,1 kHz. Seluruh durasi 18,95 detik berhasil didekode FFmpeg.
- Tombol **Simpan berkas** diuji di browser dan menghasilkan MP4 di folder Downloads Windows. Riwayat selesai tetap tampil setelah server dijalankan ulang. Pratinjau tersimpan di `docs/preview.jpg`.
- **Batas uji langsung**: stream audio terpisah pada pilihan 144p mendapat HTTP 503 dari server media. Versi final menampilkan pesan layanan sementara tidak tersedia. Penggabungan lokal lulus, tetapi unduhan adaptif dari YouTube belum terverifikasi sampai selesai pada jaringan ini.
- Satu warning dependensi pengujian: Starlette menyarankan migrasi TestClient dari `httpx` ke `httpx2`; pengujian saat ini tetap lulus.

Tes dijalankan dengan akses socket lokal diizinkan; sandbox Windows yang memblokir socket dapat membuat TestClient berhenti menunggu saat membangun event loop.
