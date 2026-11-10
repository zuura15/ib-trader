package tsnetmobile

import (
	"strings"
	"testing"
)

func TestParseConfig(t *testing.T) {
	cfg, err := parseConfig(`{"stateDir":"/data/tsnet","hostname":"phone","authKey":"","dekB64":"aa"}`)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.StateDir != "/data/tsnet" || cfg.Hostname != "phone" {
		t.Fatalf("%+v", cfg)
	}
	if _, err := parseConfig(`{"hostname":"phone"}`); err == nil {
		t.Fatal("expected missing stateDir")
	}
	if _, err := parseConfig(`not-json`); err == nil {
		t.Fatal("expected json error")
	}
}

func TestRedactAuthKey(t *testing.T) {
	got := redact("failed tskey-auth-k1234567abcdef for node")
	if strings.Contains(got, "k1234567abcdef") || !strings.Contains(got, "tskey-redacted") {
		t.Fatalf("%q", got)
	}
}
