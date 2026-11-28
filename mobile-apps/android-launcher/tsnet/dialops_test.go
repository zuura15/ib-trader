package tsnetmobile

import (
	"bufio"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"fmt"
	"net"
	"net/http"
	"strconv"
	"strings"
	"testing"
	"time"

	"golang.org/x/crypto/ssh"
)

func TestFetchAndProbe(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	go func() {
		conn, err := ln.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		br := bufio.NewReader(conn)
		req, err := http.ReadRequest(br)
		if err != nil {
			return
		}
		body := "hello " + req.URL.Path
		fmt.Fprintf(conn, "HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s", len(body), body)
	}()
	dial := func(ctx context.Context, network, addr string) (net.Conn, error) {
		var d net.Dialer
		return d.DialContext(ctx, network, addr)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	got := fetchURL(ctx, dial, "http://"+ln.Addr().String()+"/health")
	if !got.OK || got.Status != 200 || !strings.Contains(got.Body, "/health") {
		t.Fatalf("%+v", got)
	}
	bad := fetchURL(ctx, dial, "ftp://example")
	if bad.OK || bad.Error == "" {
		t.Fatalf("%+v", bad)
	}

	host, portStr, _ := net.SplitHostPort(ln.Addr().String())
	// The HTTP server accepted one connection. Probe a fresh listener.
	echo, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer echo.Close()
	go func() {
		c, err := echo.Accept()
		if err != nil {
			return
		}
		defer c.Close()
		_, _ = c.Write([]byte("SSH-2.0-test\r\n"))
	}()
	eh, ep, _ := net.SplitHostPort(echo.Addr().String())
	pn, _ := strconv.Atoi(ep)
	pr := probeTCP(ctx, dial, eh, pn)
	if !pr.OK || !strings.Contains(pr.Banner, "SSH-2.0") {
		t.Fatalf("%+v", pr)
	}
	_ = host
	_ = portStr
}

func TestSSHExec(t *testing.T) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	signer, err := ssh.NewSignerFromKey(key)
	if err != nil {
		t.Fatal(err)
	}
	cfg := &ssh.ServerConfig{
		PasswordCallback: func(meta ssh.ConnMetadata, pass []byte) (*ssh.Permissions, error) {
			if meta.User() == "alice" && string(pass) == "s3cret" {
				return nil, nil
			}
			return nil, fmt.Errorf("denied")
		},
	}
	cfg.AddHostKey(signer)
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	go serveOneSSH(ln, cfg)

	host, portStr, _ := net.SplitHostPort(ln.Addr().String())
	port, _ := strconv.Atoi(portStr)
	dial := func(ctx context.Context, network, addr string) (net.Conn, error) {
		var d net.Dialer
		return d.DialContext(ctx, network, addr)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	got := sshExec(ctx, dial, host, port, "alice", "s3cret", "uname")
	if !got.OK || !strings.Contains(got.Stdout, "ran:uname") {
		t.Fatalf("%+v", got)
	}
	if strings.Contains(mustJSON(got), "s3cret") {
		t.Fatal("password leaked into the result")
	}
}

func TestSSHExecRejectsEmptyPassword(t *testing.T) {
	got := sshExec(context.Background(), nil, "h", 22, "alice", "", "true")
	if got.OK || got.Error == "" {
		t.Fatalf("%+v", got)
	}
}

func serveOneSSH(ln net.Listener, cfg *ssh.ServerConfig) {
	conn, err := ln.Accept()
	if err != nil {
		return
	}
	sc, chans, reqs, err := ssh.NewServerConn(conn, cfg)
	if err != nil {
		conn.Close()
		return
	}
	defer sc.Close()
	go ssh.DiscardRequests(reqs)
	for ch := range chans {
		if ch.ChannelType() != "session" {
			_ = ch.Reject(ssh.UnknownChannelType, "unsupported")
			continue
		}
		channel, reqs, err := ch.Accept()
		if err != nil {
			continue
		}
		go func() {
			defer channel.Close()
			for req := range reqs {
				if req.Type == "exec" {
					var payload struct{ Command string }
					_ = ssh.Unmarshal(req.Payload, &payload)
					_ = req.Reply(true, nil)
					fmt.Fprintf(channel, "ran:%s", payload.Command)
					_, _ = channel.SendRequest("exit-status", false, ssh.Marshal(struct{ Status uint32 }{0}))
					return
				}
				_ = req.Reply(true, nil)
			}
		}()
	}
}
