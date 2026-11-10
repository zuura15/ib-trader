# Credentials at rest

Two keys are involved.

- An AES-256-GCM key generated in the Android Keystore (`AndroidKeystoreAead`, alias `ib-launcher-vault`). It is not exportable. It seals the auth key, the settings blob (origin, bearer, SSH password), a registration flag, plaintext sidecars, and the data key below.
- A random 32-byte data key (DEK), wrapped by the Keystore key and stored as `files/vault/dek.bin`. The Go `aesStore` uses the unwrapped DEK only in memory. Each tsnet state record (`_machinekey`, profiles, the node key) is written under `files/tsnet/` as `<sha256>.aead` (`AE1` magic, nonce, ciphertext). tsnet is given that store via `Server.Store`, so it does not write `tailscaled.state` in the clear.

`tailscaled.log.conf` holds a log id. A few seconds after start the app seals that file into `vault/sidecars.bin` and deletes the plaintext. On stop, every remaining non-`.aead` file in the state directory is sealed the same way and removed. Restore runs before the next start and only fills files that are missing.

While the process is running, filch log buffers may still sit in the app-private directory. The node key and machine key do not. App-private storage is also credential-encrypted by Android when the device has a lock screen. Backup and device-transfer rules exclude the files, and `allowBackup` is false. Uninstall deletes the Keystore key, so the sealed state cannot be decrypted after a reinstall; sign in again.

Clearing credentials logs the node out, deletes the auth key, the DEK, the registration flag, sidecars, and the state directory. Launcher settings (origin, SSH) stay. The next launch is the first-launch screen.

Automatic start calls `Startup.decide`. Both the registration flag and at least one `.aead` file are required. The auth key sent to tsnet comes from `Startup.authKeyFor`: an explicit login action may pass a key, and an automatic reconnect passes an empty string even if a key is still stored.
