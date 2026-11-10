// Package tsnetmobile embeds a Tailscale tsnet node for the Android launcher.
//
// The node uses userspace networking only. Server.Tun is left nil, so
// tsnet's netstack carries this process's sockets and the phone does not
// get a VpnService or a TUN interface. Kotlin reaches the tailnet through
// the loopback HTTP proxy (page, REST, and WebSocket CONNECT) and through
// Fetch, ProbeTCP, and SSHExec.
package tsnetmobile

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"tailscale.com/ipn"
	"tailscale.com/tsnet"
)

var (
	loginURLRe = regexp.MustCompile(`https://\S+`)
	tskeyRe    = regexp.MustCompile(`tskey-[A-Za-z0-9\-]+`)
)

// Node is the gomobile handle. The live state sits in an unexported map
// so the exported struct stays a single int64, which gomobile can bind.
type Node struct {
	ID int64
}

type startConfig struct {
	StateDir   string `json:"stateDir"`
	Hostname   string `json:"hostname"`
	AuthKey    string `json:"authKey"`
	ControlURL string `json:"controlUrl"`
	Origin     string `json:"origin"`
	Bearer     string `json:"bearerToken"`
	DekB64     string `json:"dekB64"`
}

type peerOut struct {
	Name    string   `json:"name"`
	DNSName string   `json:"dnsName"`
	Online  bool     `json:"online"`
	OS      string   `json:"os"`
	IPs     []string `json:"ips"`
}

type statusSnapshot struct {
	BackendState string    `json:"backendState"`
	LoggedIn     bool      `json:"loggedIn"`
	LoginURL     string    `json:"loginUrl"`
	Hostname     string    `json:"hostname"`
	DNSName      string    `json:"dnsName"`
	IPs          []string  `json:"ips"`
	KeyExpiry    string    `json:"keyExpiry"`
	LastError    string    `json:"lastError"`
	TUN          bool      `json:"tun"`
	Userspace    bool      `json:"userspace"`
	VpnService   bool      `json:"vpnService"`
	ProxyPort    int       `json:"proxyPort"`
	Peers        []peerOut `json:"peers"`
	NodeRunning  bool      `json:"nodeRunning"`
}

type nodeCore struct {
	srv      *tsnet.Server
	proxy    *proxyServer
	done     chan struct{}
	cancel   context.CancelFunc
	hostname string

	mu       sync.Mutex
	snap     statusSnapshot
	loginURL string
	lastErr  string
}

var (
	regMu sync.Mutex
	reg   = map[int64]*nodeCore{}
	next  int64
)

// Start brings up the userspace node and the loopback proxy.
// configJSON is produced by the Kotlin launcher. authKey is empty on
// every automatic reconnect; the persisted node state is enough.
func Start(configJSON string) (*Node, error) {
	cfg, err := parseConfig(configJSON)
	if err != nil {
		return nil, err
	}
	dek, err := base64.StdEncoding.DecodeString(strings.TrimSpace(cfg.DekB64))
	if err != nil || len(dek) != 32 {
		return nil, fmt.Errorf("dekB64 must be standard base64 of 32 bytes")
	}
	if err := os.MkdirAll(cfg.StateDir, 0o700); err != nil {
		return nil, err
	}
	// A one-shot key in the environment must not authenticate a later launch.
	if strings.TrimSpace(cfg.AuthKey) == "" {
		_ = os.Unsetenv("TS_AUTHKEY")
		_ = os.Unsetenv("TS_AUTH_KEY")
		_ = os.Unsetenv("TSNET_FORCE_LOGIN")
	}
	store, err := newAesStore(cfg.StateDir, dek)
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithCancel(context.Background())
	core := &nodeCore{
		done:     make(chan struct{}),
		cancel:   cancel,
		hostname: cfg.Hostname,
		snap: statusSnapshot{
			BackendState: "Starting",
			Userspace:    true,
			VpnService:   false,
			Hostname:     cfg.Hostname,
			NodeRunning:  true,
			IPs:          []string{},
			Peers:        []peerOut{},
		},
	}
	srv := &tsnet.Server{
		Dir:        cfg.StateDir,
		Hostname:   cfg.Hostname,
		AuthKey:    strings.TrimSpace(cfg.AuthKey),
		ControlURL: strings.TrimSpace(cfg.ControlURL),
		Store:      store,
		UserLogf:   core.userLog,
		Logf:       func(string, ...any) {},
		// Tun stays nil: userspace netstack only. No VpnService.
	}
	if err := ensureUserspace(srv.Tun != nil); err != nil {
		cancel()
		return nil, err
	}
	core.srv = srv
	if err := srv.Start(); err != nil {
		cancel()
		return nil, err
	}
	proxy, err := newProxy(func(ctx context.Context, network, addr string) (net.Conn, error) {
		return srv.Dial(ctx, network, addr)
	})
	if err != nil {
		_ = srv.Close()
		cancel()
		return nil, err
	}
	proxy.setRoute(cfg.Origin, cfg.Bearer)
	core.proxy = proxy
	core.mu.Lock()
	core.snap.ProxyPort = proxy.port()
	core.mu.Unlock()
	go core.watch(ctx)
	go core.poll()
	return register(core), nil
}

func parseConfig(raw string) (startConfig, error) {
	var cfg startConfig
	if err := json.Unmarshal([]byte(raw), &cfg); err != nil {
		return cfg, err
	}
	cfg.StateDir = strings.TrimSpace(cfg.StateDir)
	cfg.Hostname = strings.TrimSpace(cfg.Hostname)
	if cfg.StateDir == "" {
		return cfg, fmt.Errorf("stateDir is required")
	}
	if cfg.Hostname == "" {
		return cfg, fmt.Errorf("hostname is required")
	}
	return cfg, nil
}

func register(c *nodeCore) *Node {
	regMu.Lock()
	defer regMu.Unlock()
	next++
	reg[next] = c
	return &Node{ID: next}
}

func lookup(id int64) *nodeCore {
	regMu.Lock()
	defer regMu.Unlock()
	return reg[id]
}

func (n *Node) Stop() {
	if n == nil {
		return
	}
	regMu.Lock()
	c := reg[n.ID]
	delete(reg, n.ID)
	regMu.Unlock()
	if c == nil {
		return
	}
	select {
	case <-c.done:
	default:
		close(c.done)
	}
	if c.cancel != nil {
		c.cancel()
	}
	if c.proxy != nil {
		c.proxy.close()
	}
	if c.srv != nil {
		_ = c.srv.Close()
	}
}

// StatusJSON returns the cached tailnet snapshot. Kotlin polls it.
func (n *Node) StatusJSON() string {
	c := lookup(n.ID)
	if c == nil {
		return mustJSON(statusSnapshot{
			BackendState: "Stopped",
			Userspace:    true,
			LastError:    "node is stopped",
			IPs:          []string{},
			Peers:        []peerOut{},
		})
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	return mustJSON(c.snap)
}

// BeginInteractiveLogin asks the control plane for a login URL.
// The URL shows up in a later StatusJSON. Returns the URL if one is
// already known.
func (n *Node) BeginInteractiveLogin() string {
	c := lookup(n.ID)
	if c == nil {
		return ""
	}
	lc, err := c.srv.LocalClient()
	if err != nil {
		c.setError(err.Error())
		return ""
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	if err := lc.StartLoginInteractive(ctx); err != nil {
		c.setError(err.Error())
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.loginURL
}

// UpdateRoute changes the origin that receives the optional bearer token
// without restarting the node.
func (n *Node) UpdateRoute(origin, bearer string) {
	c := lookup(n.ID)
	if c == nil || c.proxy == nil {
		return
	}
	c.proxy.setRoute(origin, bearer)
}

// Fetch GETs a URL through the tailnet and returns a JSON result.
func (n *Node) Fetch(rawURL string) string {
	c := lookup(n.ID)
	if c == nil {
		return mustJSON(fetchResult{URL: rawURL, Error: "node is stopped"})
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	return mustJSON(fetchURL(ctx, c.srv.Dial, rawURL))
}

// ProbeTCP opens a TCP connection through the tailnet and returns JSON.
func (n *Node) ProbeTCP(host string, port int32) string {
	c := lookup(n.ID)
	if c == nil {
		return mustJSON(probeResult{Error: "node is stopped", Addr: host})
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	return mustJSON(probeTCP(ctx, c.srv.Dial, host, int(port)))
}

// SSHExec runs one command over SSH through the tailnet. Host keys are
// not verified; the Android UI says so.
func (n *Node) SSHExec(host string, port int32, user, password, command string) string {
	c := lookup(n.ID)
	if c == nil {
		return mustJSON(sshResult{Error: "node is stopped"})
	}
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
	defer cancel()
	return mustJSON(sshExec(ctx, c.srv.Dial, host, int(port), user, password, command))
}

// Logout asks the local backend to log out. Kotlin still wipes the
// state directory afterwards; logout alone is not the credential clear.
func (n *Node) Logout() {
	c := lookup(n.ID)
	if c == nil {
		return
	}
	lc, err := c.srv.LocalClient()
	if err != nil {
		c.setError(err.Error())
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := lc.Logout(ctx); err != nil {
		c.setError(err.Error())
	}
}

func (c *nodeCore) userLog(format string, args ...any) {
	msg := redact(fmt.Sprintf(format, args...))
	if u := loginURLRe.FindString(msg); u != "" {
		c.mu.Lock()
		c.loginURL = strings.TrimRight(u, ").,")
		c.snap.LoginURL = c.loginURL
		c.mu.Unlock()
	}
}

func (c *nodeCore) setError(msg string) {
	c.mu.Lock()
	c.lastErr = redact(msg)
	c.snap.LastError = c.lastErr
	c.mu.Unlock()
}

func (c *nodeCore) watch(ctx context.Context) {
	lc, err := c.srv.LocalClient()
	if err != nil {
		c.setError(err.Error())
		return
	}
	watcher, err := lc.WatchIPNBus(ctx, ipn.NotifyInitialState)
	if err != nil {
		c.setError(err.Error())
		return
	}
	defer watcher.Close()
	for {
		n, err := watcher.Next()
		if err != nil {
			return
		}
		if n.BrowseToURL != nil && *n.BrowseToURL != "" {
			c.mu.Lock()
			c.loginURL = *n.BrowseToURL
			c.snap.LoginURL = c.loginURL
			c.mu.Unlock()
		}
		if n.ErrMessage != nil && *n.ErrMessage != "" {
			c.setError(*n.ErrMessage)
		}
	}
}

func (c *nodeCore) poll() {
	c.refresh()
	t := time.NewTicker(time.Second)
	defer t.Stop()
	for {
		select {
		case <-c.done:
			return
		case <-t.C:
			c.refresh()
		}
	}
}

func (c *nodeCore) refresh() {
	select {
	case <-c.done:
		return
	default:
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	lc, err := c.srv.LocalClient()
	if err != nil {
		c.setError(err.Error())
		return
	}
	st, err := lc.Status(ctx)
	if err != nil {
		c.setError(err.Error())
		return
	}
	ips := make([]string, 0, len(st.TailscaleIPs))
	for _, ip := range st.TailscaleIPs {
		ips = append(ips, ip.String())
	}
	peers := make([]peerOut, 0)
	for _, ps := range st.Peer {
		if ps == nil {
			continue
		}
		pips := make([]string, 0, len(ps.TailscaleIPs))
		for _, ip := range ps.TailscaleIPs {
			pips = append(pips, ip.String())
		}
		name := ps.HostName
		if name == "" {
			name = strings.TrimSuffix(ps.DNSName, ".")
		}
		peers = append(peers, peerOut{
			Name:    name,
			DNSName: strings.TrimSuffix(ps.DNSName, "."),
			Online:  ps.Online,
			OS:      ps.OS,
			IPs:     pips,
		})
	}
	sort.Slice(peers, func(i, j int) bool { return peers[i].Name < peers[j].Name })
	if len(peers) > 64 {
		peers = peers[:64]
	}
	host := c.hostname
	dnsName := ""
	keyExpiry := ""
	if st.Self != nil {
		if st.Self.HostName != "" {
			host = st.Self.HostName
		}
		dnsName = strings.TrimSuffix(st.Self.DNSName, ".")
		if st.Self.KeyExpiry != nil && !st.Self.KeyExpiry.IsZero() {
			keyExpiry = st.Self.KeyExpiry.UTC().Format(time.RFC3339)
		}
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if st.AuthURL != "" {
		c.loginURL = st.AuthURL
	}
	last := c.lastErr
	if len(st.Health) > 0 {
		last = redact(strings.Join(st.Health, "; "))
	} else if st.BackendState == "Running" {
		last = ""
		c.lastErr = ""
	}
	c.snap = statusSnapshot{
		BackendState: st.BackendState,
		LoggedIn:     st.BackendState == "Running",
		LoginURL:     c.loginURL,
		Hostname:     host,
		DNSName:      dnsName,
		IPs:          ips,
		KeyExpiry:    keyExpiry,
		LastError:    last,
		TUN:          st.TUN,
		Userspace:    !st.TUN,
		VpnService:   false,
		ProxyPort:    c.proxy.port(),
		Peers:        peers,
		NodeRunning:  true,
	}
}

func redact(s string) string {
	return tskeyRe.ReplaceAllString(s, "tskey-redacted")
}
