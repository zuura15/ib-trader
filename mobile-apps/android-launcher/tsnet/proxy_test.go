package tsnetmobile

import (
	"bufio"
	"context"
	"crypto/sha1"
	"encoding/base64"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

func TestProxyListensOnLoopbackAndForwardsHTTP(t *testing.T) {
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("Authorization"); got != "Bearer secret" {
			t.Errorf("authorization = %q", got)
		}
		if r.URL.Query().Get("token") != "" {
			t.Errorf("REST path should not gain a ws token, got %q", r.URL.RawQuery)
		}
		fmt.Fprint(w, "ok-body")
	}))
	defer backend.Close()

	other := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "" {
			t.Errorf("unexpected authorization on other host")
		}
		fmt.Fprint(w, "other")
	}))
	defer other.Close()

	p := testProxy(t)
	p.setRoute(backend.URL, "secret")
	if !strings.HasPrefix(p.ln.Addr().String(), "127.0.0.1:") {
		t.Fatalf("proxy addr %s", p.ln.Addr())
	}

	proxyURL, err := url.Parse("http://" + p.ln.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	client := &http.Client{Transport: &http.Transport{Proxy: http.ProxyURL(proxyURL)}}
	resp, err := client.Get(backend.URL + "/api/status")
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 200 || string(body) != "ok-body" {
		t.Fatalf("status %d body %q", resp.StatusCode, body)
	}

	resp, err = client.Get(other.URL + "/x")
	if err != nil {
		t.Fatal(err)
	}
	body, _ = io.ReadAll(resp.Body)
	resp.Body.Close()
	if string(body) != "other" {
		t.Fatalf("body %q", body)
	}
}

func TestWebSocketThroughConnectInjectsBearerAndToken(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	errCh := make(chan error, 1)
	go func() {
		conn, err := ln.Accept()
		if err != nil {
			errCh <- err
			return
		}
		defer conn.Close()
		br := bufio.NewReader(conn)
		req, err := http.ReadRequest(br)
		if err != nil {
			errCh <- err
			return
		}
		if req.URL.Path != "/ws" {
			errCh <- fmt.Errorf("path %s", req.URL.Path)
			return
		}
		if req.Header.Get("Authorization") != "Bearer secret" {
			errCh <- fmt.Errorf("authorization %q", req.Header.Get("Authorization"))
			return
		}
		if req.URL.Query().Get("token") != "secret" {
			errCh <- fmt.Errorf("token %q", req.URL.RawQuery)
			return
		}
		key := req.Header.Get("Sec-WebSocket-Key")
		sum := sha1.Sum([]byte(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"))
		accept := base64.StdEncoding.EncodeToString(sum[:])
		fmt.Fprintf(conn, "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n", accept)
		hdr := make([]byte, 2)
		if _, err := io.ReadFull(br, hdr); err != nil {
			errCh <- err
			return
		}
		n := int(hdr[1] & 0x7f)
		mask := make([]byte, 4)
		payload := make([]byte, n)
		if _, err := io.ReadFull(br, mask); err != nil {
			errCh <- err
			return
		}
		if _, err := io.ReadFull(br, payload); err != nil {
			errCh <- err
			return
		}
		for i := range payload {
			payload[i] ^= mask[i%4]
		}
		if string(payload) != "ping" {
			errCh <- fmt.Errorf("payload %q", payload)
			return
		}
		out := append([]byte{0x81, byte(len(payload))}, payload...)
		_, err = conn.Write(out)
		errCh <- err
	}()

	p := testProxy(t)
	p.setRoute("http://"+ln.Addr().String(), "secret")
	conn, err := net.Dial("tcp", p.ln.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
	dest := ln.Addr().String()
	fmt.Fprintf(conn, "CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n", dest, dest)
	br := bufio.NewReader(conn)
	status, err := readStatusLine(br)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(status, "200") {
		t.Fatalf("connect status %q", status)
	}
	if err := discardHeaders(br); err != nil {
		t.Fatal(err)
	}
	fmt.Fprintf(conn, "GET /ws HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n", dest)
	wsStatus, err := readStatusLine(br)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(wsStatus, "101") {
		t.Fatalf("ws status %q", wsStatus)
	}
	if err := discardHeaders(br); err != nil {
		t.Fatal(err)
	}
	frame := []byte{0x81, 0x84}
	mask := []byte{1, 2, 3, 4}
	payload := []byte("ping")
	frame = append(frame, mask...)
	for i, b := range payload {
		frame = append(frame, b^mask[i%4])
	}
	if _, err := conn.Write(frame); err != nil {
		t.Fatal(err)
	}
	echo := make([]byte, 2+len(payload))
	if _, err := io.ReadFull(br, echo); err != nil {
		t.Fatal(err)
	}
	if string(echo[2:]) != "ping" {
		t.Fatalf("echo %q", echo)
	}
	if err := <-errCh; err != nil {
		t.Fatal(err)
	}
}

func TestConnectRawBytes(t *testing.T) {
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
		buf := make([]byte, 16)
		n, _ := conn.Read(buf)
		_, _ = conn.Write(buf[:n])
	}()
	p := testProxy(t)
	conn, err := net.Dial("tcp", p.ln.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
	dest := ln.Addr().String()
	fmt.Fprintf(conn, "CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n", dest, dest)
	br := bufio.NewReader(conn)
	if _, err := readStatusLine(br); err != nil {
		t.Fatal(err)
	}
	if err := discardHeaders(br); err != nil {
		t.Fatal(err)
	}
	if _, err := conn.Write([]byte{0x16, 0x03, 0x01, 9, 9}); err != nil {
		t.Fatal(err)
	}
	got := make([]byte, 5)
	if _, err := io.ReadFull(br, got); err != nil {
		t.Fatal(err)
	}
	if got[0] != 0x16 || got[3] != 9 {
		t.Fatalf("raw echo %v", got)
	}
}

func testProxy(t *testing.T) *proxyServer {
	t.Helper()
	p, err := newProxy(func(ctx context.Context, network, addr string) (net.Conn, error) {
		var d net.Dialer
		return d.DialContext(ctx, network, addr)
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(p.close)
	return p
}

func readStatusLine(r *bufio.Reader) (string, error) {
	line, err := r.ReadString('\n')
	return strings.TrimSpace(line), err
}

func discardHeaders(r *bufio.Reader) error {
	for {
		line, err := r.ReadString('\n')
		if err != nil {
			return err
		}
		if line == "\r\n" || line == "\n" {
			return nil
		}
	}
}
