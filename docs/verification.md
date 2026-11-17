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

## Versi 1.1 — Vercel gateway + Docker worker

- **33 tes lulus**: seluruh alur sebelumnya ditambah gateway → worker, berkas 6.553.600 byte yang diunduh langsung dari worker, HTTP Range 206, signed URL kedaluwarsa/rusak, isolasi sesi browser, autentikasi API worker, antrean per sesi, rate limit cek video, disk penuh, dan pemulihan job tersimpan sesudah restart.
- Uji browser dengan gateway port 8020 dan worker port 8010 berhasil mengambil kualitas, membuat antrean, dan menyelesaikan unduhan YouTube asli: MP4 `Me at the zoo`, 629.172 byte. Tidak ada error/warning pada console browser. Pratinjau tersimpan di `docs/preview-online.jpg`. Ini menguji aplikasi native yang sama dengan image Docker; bukan bukti container sudah dijalankan.
- Import `index.py` diuji dalam proses Python baru: tidak mengimpor yt-dlp, imageio-ffmpeg, atau service SQLite, serta tidak membuat direktori unduhan/database. Gateway tanpa konfigurasi worker tetap menyajikan halaman dan memberikan status layanan belum tersedia.
- `node --check`, `pip check`, dan pemeriksaan whitespace Git lulus. Compose YAML dan Vercel JSON berhasil diparse; volume persisten, secret wajib, serta profil proxy HTTPS diperiksa.
- **Batas verifikasi Docker**: Docker Engine/CLI tidak terpasang di lingkungan ini. Image belum dibangun/dijalankan dan penerbitan sertifikat proxy belum diuji. Pengujian gateway/worker menggunakan aplikasi Python yang sama secara native dan transport ASGI dalam tes.
- **Status deployment**: belum ada server Docker publik atau deployment Vercel yang dibuat. Operator perlu menjalankan worker, menyediakan domain HTTPS, lalu memasang `ALIR_BACKEND_URL` dan `ALIR_WORKER_TOKEN` di Vercel.
