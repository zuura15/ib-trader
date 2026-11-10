package tsnetmobile

import (
	"bufio"
	"context"
	"crypto/tls"
	"io"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"
)

// dialFunc dials through the tailnet. Tests substitute a direct dialer.
type dialFunc func(ctx context.Context, network, addr string) (net.Conn, error)

// proxyServer is an HTTP forward proxy and CONNECT tunnel bound to
// 127.0.0.1. Chromium WebView sends ordinary HTTP (page, assets, /api)
// as proxy requests and sends WebSocket upgrades through CONNECT, which
// is what ProxyController's http:// rule selects.
type proxyServer struct {
	ln        net.Listener
	http      *http.Server
	dial      dialFunc
	transport *http.Transport

	mu         sync.Mutex
	originHost string
	originPort int
	bearer     string
}

func newProxy(dial dialFunc) (*proxyServer, error) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return nil, err
	}
	p := &proxyServer{ln: ln, dial: dial}
	p.transport = &http.Transport{
		DialContext:           dial,
		ForceAttemptHTTP2:     false,
		TLSNextProto:          map[string]func(string, *tls.Conn) http.RoundTripper{},
		ResponseHeaderTimeout: 30 * time.Second,
		IdleConnTimeout:       90 * time.Second,
	}
	p.http = &http.Server{
		Handler:           http.HandlerFunc(p.serveHTTP),
		ReadHeaderTimeout: 20 * time.Second,
	}
	go func() { _ = p.http.Serve(ln) }()
	return p, nil
}

func (p *proxyServer) port() int {
	return p.ln.Addr().(*net.TCPAddr).Port
}

func (p *proxyServer) close() {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	_ = p.http.Shutdown(ctx)
	_ = p.ln.Close()
	p.transport.CloseIdleConnections()
}

func (p *proxyServer) setRoute(origin, bearer string) {
	host, port := parseOriginEndpoint(origin)
	p.mu.Lock()
	p.originHost = host
	p.originPort = port
	p.bearer = strings.TrimSpace(bearer)
	p.mu.Unlock()
}

func (p *proxyServer) route() (host string, port int, bearer string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.originHost, p.originPort, p.bearer
}

func parseOriginEndpoint(origin string) (string, int) {
	origin = strings.TrimSpace(origin)
	if origin == "" {
		return "", 0
	}
	u, err := url.Parse(origin)
	if err != nil || u.Hostname() == "" {
		return "", 0
	}
	port := 80
	if strings.EqualFold(u.Scheme, "https") {
		port = 443
	}
	if raw := u.Port(); raw != "" {
		n, err := strconv.Atoi(raw)
		if err == nil {
			port = n
		}
	}
	return u.Hostname(), port
}

func (p *proxyServer) serveHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodConnect {
		p.handleConnect(w, r)
		return
	}
	p.handleForward(w, r)
}

func (p *proxyServer) handleConnect(w http.ResponseWriter, r *http.Request) {
	dest := r.Host
	if dest == "" && r.URL != nil {
		dest = r.URL.Host
	}
	if dest == "" {
		http.Error(w, "missing connect target", http.StatusBadRequest)
		return
	}
	hj, ok := w.(http.Hijacker)
	if !ok {
		http.Error(w, "hijack not supported", http.StatusInternalServerError)
		return
	}
	upstream, err := p.dial(context.Background(), "tcp", dest)
	if err != nil {
		http.Error(w, "tailnet dial failed", http.StatusBadGateway)
		return
	}
	client, brw, err := hj.Hijack()
	if err != nil {
		upstream.Close()
		return
	}
	defer client.Close()
	defer upstream.Close()
	if _, err := io.WriteString(brw, "HTTP/1.1 200 Connection Established\r\n\r\n"); err != nil {
		return
	}
	if err := brw.Flush(); err != nil {
		return
	}
	p.bridge(brw.Reader, client, upstream, dest)
}

func (p *proxyServer) bridge(reader *bufio.Reader, client net.Conn, upstream net.Conn, dest string) {
	peek, err := reader.Peek(1)
	if err != nil {
		return
	}
	if !isHTTPMethodByte(peek[0]) {
		errc := make(chan struct{}, 2)
		go func() { _, _ = io.Copy(upstream, reader); errc <- struct{}{} }()
		go func() { _, _ = io.Copy(client, upstream); errc <- struct{}{} }()
		<-errc
		return
	}
	req, err := http.ReadRequest(reader)
	if err != nil {
		return
	}
	p.inject(req, dest)
	if err := req.Write(upstream); err != nil {
		return
	}
	errc := make(chan struct{}, 2)
	go func() { _, _ = io.Copy(upstream, reader); errc <- struct{}{} }()
	go func() { _, _ = io.Copy(client, upstream); errc <- struct{}{} }()
	<-errc
}

func isHTTPMethodByte(b byte) bool {
	switch b {
	case 'G', 'P', 'H', 'D', 'O', 'C', 'T', 'U':
		return true
	default:
		return false
	}
}

func (p *proxyServer) handleForward(w http.ResponseWriter, r *http.Request) {
	if r.URL == nil || r.URL.Host == "" {
		http.Error(w, "proxy requires an absolute URL", http.StatusBadRequest)
		return
	}
	dest := hostPort(r.URL)
	if isUpgrade(r) {
		p.forwardUpgrade(w, r, dest)
		return
	}
	out := r.Clone(r.Context())
	out.RequestURI = ""
	stripHop(out.Header)
	p.inject(out, dest)
	resp, err := p.transport.RoundTrip(out)
	if err != nil {
		http.Error(w, "tailnet dial failed", http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()
	copyHeader(w.Header(), resp.Header)
	w.WriteHeader(resp.StatusCode)
	_, _ = io.Copy(w, resp.Body)
}

func (p *proxyServer) forwardUpgrade(w http.ResponseWriter, r *http.Request, dest string) {
	hj, ok := w.(http.Hijacker)
	if !ok {
		http.Error(w, "hijack not supported", http.StatusInternalServerError)
		return
	}
	upstream, err := p.dial(context.Background(), "tcp", dest)
	if err != nil {
		http.Error(w, "tailnet dial failed", http.StatusBadGateway)
		return
	}
	out := r.Clone(context.Background())
	out.RequestURI = ""
	p.inject(out, dest)
	if err := out.Write(upstream); err != nil {
		upstream.Close()
		http.Error(w, "tailnet write failed", http.StatusBadGateway)
		return
	}
	client, brw, err := hj.Hijack()
	if err != nil {
		upstream.Close()
		return
	}
	defer client.Close()
	defer upstream.Close()
	errc := make(chan struct{}, 2)
	go func() { _, _ = io.Copy(upstream, brw.Reader); errc <- struct{}{} }()
	go func() { _, _ = io.Copy(client, upstream); errc <- struct{}{} }()
	<-errc
}

func (p *proxyServer) inject(req *http.Request, dest string) {
	host, port, bearer := p.route()
	if bearer == "" || !endpointMatch(dest, host, port) {
		return
	}
	if req.Header.Get("Authorization") == "" {
		req.Header.Set("Authorization", "Bearer "+bearer)
	}
	path := ""
	if req.URL != nil {
		path = req.URL.Path
	}
	if path == "/ws" || strings.HasPrefix(path, "/ws/") {
		q := req.URL.Query()
		if q.Get("token") == "" {
			q.Set("token", bearer)
			req.URL.RawQuery = q.Encode()
		}
	}
}

func endpointMatch(dest, originHost string, originPort int) bool {
	if originHost == "" || originPort == 0 {
		return false
	}
	host, portStr, err := net.SplitHostPort(dest)
	if err != nil {
		return strings.EqualFold(dest, originHost) && originPort == 80
	}
	port, err := strconv.Atoi(portStr)
	if err != nil {
		return false
	}
	return strings.EqualFold(host, originHost) && port == originPort
}

func hostPort(u *url.URL) string {
	if u.Port() != "" {
		return u.Host
	}
	if strings.EqualFold(u.Scheme, "https") {
		return net.JoinHostPort(u.Hostname(), "443")
	}
	return net.JoinHostPort(u.Hostname(), "80")
}

func isUpgrade(r *http.Request) bool {
	return strings.Contains(strings.ToLower(r.Header.Get("Connection")), "upgrade") ||
		r.Header.Get("Upgrade") != ""
}

func stripHop(h http.Header) {
	for _, k := range []string{"Proxy-Connection", "Proxy-Authenticate", "Proxy-Authorization", "Te", "Trailer"} {
		h.Del(k)
	}
}

func copyHeader(dst, src http.Header) {
	for k, vs := range src {
		for _, v := range vs {
			dst.Add(k, v)
		}
	}
}
