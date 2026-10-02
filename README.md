# Torrent Studio Vast Serverless worker

Custom Vast.ai PyWorker backend for Torrent Studio. It downloads temporary public relay URLs, runs the managed NVENC FFmpeg command, exposes progress, and streams encoded outputs back to the origin server.

The worker deliberately has no MEGA/S3 credentials. Output publication is performed by Torrent Studio after it downloads and verifies each result.

The startup installs and verifies `Noto Sans Myanmar`; the worker refuses an encode rather than letting libass render missing Myanmar glyphs as square boxes.
