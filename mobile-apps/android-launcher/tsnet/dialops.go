package tsnetmobile

import (
	"bufio"
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"golang.org/x/crypto/ssh"
)

type fetchResult struct {
	OK        bool   `json:"ok"`
	Status    int    `json:"status"`
	ElapsedMs int64  `json:"elapsedMs"`
	Body      string `json:"body"`
	Error     string `json:"error,omitempty"`
	URL       string `json:"url"`
}

type probeResult struct {
	OK     bool   `json:"ok"`
	Banner string `json:"banner,omitempty"`
	Error  string `json:"error,omitempty"`
	Addr   string `json:"addr"`
}

type sshResult struct {
	OK     bool   `json:"ok"`
	Stdout string `json:"stdout,omitempty"`
	Stderr string `json:"stderr,omitempty"`
	Error  string `json:"error,omitempty"`
}

func fetchURL(ctx context.Context, dial dialFunc, raw string) fetchResult {
	start := time.Now()
	out := fetchResult{URL: raw}
	u, err := url.Parse(raw)
	if err != nil || u.Hostname() == "" || (u.Scheme != "http" && u.Scheme != "https") {
		out.Error = "url must be http or https"
		return out
	}
	port := u.Port()
	if port == "" {
		if u.Scheme == "https" {
			port = "443"
		} else {
			port = "80"
		}
	}
	conn, err := dial(ctx, "tcp", net.JoinHostPort(u.Hostname(), port))
	if err != nil {
		out.Error = err.Error()
		out.ElapsedMs = time.Since(start).Milliseconds()
		return out
	}
	defer conn.Close()
	if dl, ok := ctx.Deadline(); ok {
		_ = conn.SetDeadline(dl)
	}
	if u.Scheme == "https" {
		tlsConn := tls.Client(conn, &tls.Config{ServerName: u.Hostname()})
		if err := tlsConn.HandshakeContext(ctx); err != nil {
			out.Error = err.Error()
			out.ElapsedMs = time.Since(start).Milliseconds()
			return out
		}
		conn = tlsConn
	}
	req, err := http.NewRequest(http.MethodGet, u.String(), nil)
	if err != nil {
		out.Error = err.Error()
		return out
	}
	req.Header.Set("User-Agent", "ib-launcher")
	req.Header.Set("Connection", "close")
	if err := req.Write(conn); err != nil {
		out.Error = err.Error()
		out.ElapsedMs = time.Since(start).Milliseconds()
		return out
	}
	resp, err := http.ReadResponse(bufio.NewReader(conn), req)
	if err != nil {
		out.Error = err.Error()
		out.ElapsedMs = time.Since(start).Milliseconds()
		return out
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	out.OK = true
	out.Status = resp.StatusCode
	out.Body = string(body)
	out.ElapsedMs = time.Since(start).Milliseconds()
	return out
}

func probeTCP(ctx context.Context, dial dialFunc, host string, port int) probeResult {
	addr := net.JoinHostPort(host, strconv.Itoa(port))
	out := probeResult{Addr: addr}
	conn, err := dial(ctx, "tcp", addr)
	if err != nil {
		out.Error = err.Error()
		return out
	}
	defer conn.Close()
	_ = conn.SetReadDeadline(time.Now().Add(400 * time.Millisecond))
	buf := make([]byte, 256)
	n, _ := conn.Read(buf)
	out.OK = true
	out.Banner = strings.TrimSpace(string(buf[:n]))
	return out
}

func sshExec(ctx context.Context, dial dialFunc, host string, port int, user, password, command string) sshResult {
	out := sshResult{}
	if strings.TrimSpace(user) == "" || strings.TrimSpace(password) == "" {
		out.Error = "ssh user and password are required"
		return out
	}
	if strings.TrimSpace(command) == "" {
		out.Error = "ssh command is empty"
		return out
	}
	addr := net.JoinHostPort(host, strconv.Itoa(port))
	conn, err := dial(ctx, "tcp", addr)
	if err != nil {
		out.Error = err.Error()
		return out
	}
	cfg := &ssh.ClientConfig{
		User:            user,
		Auth:            []ssh.AuthMethod{ssh.Password(password)},
		HostKeyCallback: ssh.InsecureIgnoreHostKey(), // phone UI warns; see README
		Timeout:         20 * time.Second,
	}
	c, chans, reqs, err := ssh.NewClientConn(conn, addr, cfg)
	if err != nil {
		conn.Close()
		out.Error = err.Error()
		return out
	}
	client := ssh.NewClient(c, chans, reqs)
	defer client.Close()
	done := make(chan struct{})
	defer close(done)
	go func() {
		select {
		case <-ctx.Done():
			client.Close()
		case <-done:
		}
	}()
	session, err := client.NewSession()
	if err != nil {
		out.Error = err.Error()
		return out
	}
	defer session.Close()
	var stdout, stderr limitedBuf
	stdout.N = 16 << 10
	stderr.N = 16 << 10
	session.Stdout = &stdout
	session.Stderr = &stderr
	runErr := session.Run(command)
	out.Stdout = stdout.String()
	out.Stderr = stderr.String()
	if runErr != nil {
		out.Error = runErr.Error()
		return out
	}
	out.OK = true
	return out
}

type limitedBuf struct {
	bytes.Buffer
	N int
}

func (b *limitedBuf) Write(p []byte) (int, error) {
	if b.N <= 0 {
		return len(p), nil
	}
	if len(p) > b.N {
		p = p[:b.N]
	}
	n, err := b.Buffer.Write(p)
	b.N -= n
	if err != nil {
		return n, err
	}
	return len(p), nil
}

func mustJSON(v any) string {
	b, err := json.Marshal(v)
	if err != nil {
		return `{"ok":false,"error":"encode failed"}`
	}
	return string(b)
}

func fmtErr(err error) string {
	if err == nil {
		return ""
	}
	return fmt.Sprint(err)
}
