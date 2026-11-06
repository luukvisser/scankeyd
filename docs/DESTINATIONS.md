# Other destinations for the PDFs

The `drive` step is plain [rclone](https://rclone.org/), so any of its ~70
storage systems works. Make a remote with the same command as for Google Drive
(it asks for every value, stores passwords obscured and keeps them out of your
shell history):

```sh
docker compose run --rm --entrypoint rclone drive config
```

then point `DRIVE_REMOTE` at it in `scan.env`:

| Destination | rclone storage type | Asked for | `DRIVE_REMOTE` |
|---|---|---|---|
| Nextcloud or ownCloud | `webdav`, vendor `nextcloud` | URL `https://<cloud>/remote.php/dav/files/<user>`, user, an app password | `cloud:Scans` |
| a NAS share (Windows/SMB) | `smb` | host, user, password, (domain) | `nas:share/Scans` |
| S3, MinIO, Backblaze B2, Cloudflare R2 | `s3` (with its provider) | endpoint, access key, secret key | `s3:bucket/scans` |
| an SFTP server | `sftp` | host, user, a key file or password | `server:/srv/scans` |
| OneDrive, Dropbox | `onedrive`, `dropbox` | a browser login, as for Drive | `onedrive:Scans` |

The container must reach the destination; it runs on Docker's default
network, so a NAS on the home network works as well as a cloud service.

Paperless-ngx needs no rclone: use the `pdf` profile and point Paperless's
consumption folder at `scans/pdf` (Paperless recognises the text itself).
