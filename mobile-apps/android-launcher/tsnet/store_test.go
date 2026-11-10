package tsnetmobile

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"tailscale.com/ipn"
)

func TestAesStoreRoundTripAndClear(t *testing.T) {
	dir := t.TempDir()
	key := bytes.Repeat([]byte{7}, 32)
	st, err := newAesStore(dir, key)
	if err != nil {
		t.Fatal(err)
	}
	const id ipn.StateKey = "_machinekey"
	secret := []byte("node-key-secret-do-not-leak")
	if err := st.WriteState(id, secret); err != nil {
		t.Fatal(err)
	}
	matches, err := filepath.Glob(filepath.Join(dir, "*.aead"))
	if err != nil || len(matches) != 1 {
		t.Fatalf("blob files: %v %v", matches, err)
	}
	raw, err := os.ReadFile(matches[0])
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(raw, secret) {
		t.Fatal("ciphertext contains the node secret")
	}
	got, err := st.ReadState(id)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, secret) {
		t.Fatalf("got %q", got)
	}
	if _, err := st.ReadState("missing"); !errors.Is(err, ipn.ErrStateNotExist) {
		t.Fatalf("missing: %v", err)
	}
	if err := st.WriteState(id, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := st.ReadState(id); !errors.Is(err, ipn.ErrStateNotExist) {
		t.Fatalf("after delete: %v", err)
	}
}

func TestAesStoreRejectsWrongKeyAndShortDEK(t *testing.T) {
	dir := t.TempDir()
	if _, err := newAesStore(dir, []byte("short")); err == nil {
		t.Fatal("expected short dek error")
	}
	key := bytes.Repeat([]byte{1}, 32)
	st, err := newAesStore(dir, key)
	if err != nil {
		t.Fatal(err)
	}
	if err := st.WriteState("k", []byte("v")); err != nil {
		t.Fatal(err)
	}
	other, err := newAesStore(dir, bytes.Repeat([]byte{2}, 32))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := other.ReadState("k"); err == nil {
		t.Fatal("expected auth failure with the wrong dek")
	}
}

func TestEnsureUserspace(t *testing.T) {
	if err := ensureUserspace(false); err != nil {
		t.Fatal(err)
	}
	if err := ensureUserspace(true); err == nil {
		t.Fatal("expected rejection")
	}
}
