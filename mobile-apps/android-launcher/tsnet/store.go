package tsnetmobile

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"sync"

	"tailscale.com/ipn"
)

// aesStore is an ipn.StateStore that writes each value as AES-GCM
// ciphertext. The data key is wrapped by the Android Keystore in
// Kotlin and handed to the node only in memory, so the node key and
// machine key are not plaintext on disk.
type aesStore struct {
	mu  sync.Mutex
	dir string
	key []byte
}

func newAesStore(dir string, key []byte) (*aesStore, error) {
	if len(key) != 32 {
		return nil, errors.New("dek must be 32 bytes")
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	k := make([]byte, 32)
	copy(k, key)
	return &aesStore{dir: dir, key: k}, nil
}

func (s *aesStore) path(id ipn.StateKey) string {
	sum := sha256.Sum256([]byte(id))
	return filepath.Join(s.dir, hex.EncodeToString(sum[:])+".aead")
}

func (s *aesStore) ReadState(id ipn.StateKey) ([]byte, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	blob, err := os.ReadFile(s.path(id))
	if err != nil {
		if os.IsNotExist(err) {
			return nil, ipn.ErrStateNotExist
		}
		return nil, err
	}
	plain, err := decryptAEAD(s.key, []byte(id), blob)
	if err != nil {
		return nil, err
	}
	return plain, nil
}

func (s *aesStore) WriteState(id ipn.StateKey, bs []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	path := s.path(id)
	if bs == nil {
		err := os.Remove(path)
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	blob, err := encryptAEAD(s.key, []byte(id), bs)
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, blob, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func encryptAEAD(key, aad, plain []byte) ([]byte, error) {
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	nonce := make([]byte, gcm.NonceSize())
	if _, err := rand.Read(nonce); err != nil {
		return nil, err
	}
	ct := gcm.Seal(nil, nonce, plain, aad)
	out := make([]byte, 0, 4+len(nonce)+len(ct))
	out = append(out, 'A', 'E', '1', 0)
	out = append(out, nonce...)
	out = append(out, ct...)
	return out, nil
}

func decryptAEAD(key, aad, blob []byte) ([]byte, error) {
	if len(blob) < 4 || string(blob[:4]) != "AE1\x00" {
		return nil, errors.New("state blob is not AE1")
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	rest := blob[4:]
	ns := gcm.NonceSize()
	if len(rest) < ns+gcm.Overhead() {
		return nil, errors.New("state blob is truncated")
	}
	nonce, ct := rest[:ns], rest[ns:]
	plain, err := gcm.Open(nil, nonce, ct, aad)
	if err != nil {
		return nil, err
	}
	return plain, nil
}

// ensureUserspace refuses a tsnet server that was given a TUN device.
// This process must not take over the phone's network.
func ensureUserspace(tunSet bool) error {
	if tunSet {
		return errors.New("refusing to start: a TUN device is set; this app is userspace-only")
	}
	return nil
}
