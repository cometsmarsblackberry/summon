package rcongateway

import (
	"context"
	"errors"
	"io"
	"net"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/summon/agent/rcon"
)

const (
	testPassword = "owner-rcon-password"
	testActor    = "76561198000000000"
)

// fakeServer stands in for the game server: it records every console line
// and answers through reply.
type fakeServer struct {
	mu    sync.Mutex
	lines []string
	reply func(line string) (string, error)
}

func (f *fakeServer) Execute(_ context.Context, line string) (string, error) {
	f.mu.Lock()
	f.lines = append(f.lines, line)
	reply := f.reply
	f.mu.Unlock()
	if reply == nil {
		return resultMarker + "OK\n", nil
	}
	return reply(line)
}

func (f *fakeServer) received() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.lines...)
}

func startGateway(t *testing.T, upstream Executor) *Gateway {
	t.Helper()
	gateway, err := Listen(Config{
		Addr: "127.0.0.1:0", Password: testPassword, ActorSteamID: testActor,
		Upstream: upstream, Label: "test",
	})
	if err != nil {
		t.Fatalf("Listen: %v", err)
	}
	t.Cleanup(gateway.Close)
	return gateway
}

func dial(t *testing.T, gateway *Gateway) *rcon.Client {
	t.Helper()
	conn, err := net.Dial("tcp4", gateway.Addr().String())
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	client := rcon.NewClient(conn)
	t.Cleanup(func() { _ = client.Close() })
	return client
}

func testContext(t *testing.T) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	t.Cleanup(cancel)
	return ctx
}

func login(t *testing.T, gateway *Gateway) *rcon.Client {
	t.Helper()
	client := dial(t, gateway)
	if err := client.Authenticate(testContext(t), testPassword); err != nil {
		t.Fatalf("Authenticate: %v", err)
	}
	return client
}

func TestCommandIsWrappedForThePlugin(t *testing.T) {
	server := &fakeServer{reply: func(string) (string, error) {
		return "L 11/16/2026 - 12:00:00: [summon.smx] log line\n" + resultMarker + "OK\n\"mp_timelimit\" = \"30\"\n", nil
	}}
	client := login(t, startGateway(t, server))

	output, err := client.Execute(testContext(t), "mp_timelimit")
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if want := "\"mp_timelimit\" = \"30\"\n"; output != want {
		t.Fatalf("output = %q, want %q", output, want)
	}
	want := []string{"sm_summon_rcon_command " + testActor + " mp_timelimit"}
	if got := server.received(); len(got) != 1 || got[0] != want[0] {
		t.Fatalf("forwarded %q, want %q", got, want)
	}
}

func TestRefusedCommandReturnsThePluginsExplanation(t *testing.T) {
	server := &fakeServer{reply: func(string) (string, error) {
		return resultMarker + "ERROR command_not_allowed\nThat server command is not allowed.\n", nil
	}}
	client := login(t, startGateway(t, server))

	output, err := client.Execute(testContext(t), "rcon_password hunter2")
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if want := "That server command is not allowed.\n"; output != want {
		t.Fatalf("output = %q, want %q", output, want)
	}
}

func TestEveryCommandInARequestIsWrappedSeparately(t *testing.T) {
	server := &fakeServer{}
	client := login(t, startGateway(t, server))

	if _, err := client.Execute(testContext(t), "mp_tournament 1; mp_tournament_restart\nsay \"hi\""); err != nil {
		t.Fatalf("Execute: %v", err)
	}
	want := []string{
		"sm_summon_rcon_command " + testActor + " mp_tournament 1",
		"sm_summon_rcon_command " + testActor + " mp_tournament_restart",
		"sm_summon_rcon_command " + testActor + ` say "hi"`,
	}
	got := server.received()
	if len(got) != len(want) {
		t.Fatalf("forwarded %q, want %q", got, want)
	}
	for index := range want {
		if got[index] != want[index] {
			t.Fatalf("forwarded[%d] = %q, want %q", index, got[index], want[index])
		}
	}
}

// The engine splits a console line at a semicolon outside quotes or at a line
// break and would run the remainder without the plugin's checks, so nothing
// the gateway forwards may contain one.
func TestNothingForwardedCanBeSplitByTheEngine(t *testing.T) {
	requests := []string{
		`say "a; rcon_password x"`,
		`say "unterminated; rcon_password x`,
		"status;rcon_password x",
		"status\nrcon_password x",
		"status\r\nrcon_password x",
		"say a\x00; rcon_password x",
		"say tab\there",
		`say "a" ; say "b;c" ; quit`,
		";;;quit;;;",
		`"say" ";" "quit"`,
		"say // comment ; quit",
	}
	for _, request := range requests {
		server := &fakeServer{}
		client := login(t, startGateway(t, server))
		if _, err := client.Execute(testContext(t), request); err != nil {
			t.Fatalf("Execute(%q): %v", request, err)
		}
		prefix := "sm_summon_rcon_command " + testActor + " "
		for _, line := range server.received() {
			if !strings.HasPrefix(line, prefix) {
				t.Fatalf("request %q forwarded an unwrapped line %q", request, line)
			}
			if strings.ContainsAny(line, ";\n\r\x00\t") {
				t.Fatalf("request %q forwarded a splittable line %q", request, line)
			}
		}
	}
}

func TestQuotedSemicolonIsRefusedNotForwarded(t *testing.T) {
	server := &fakeServer{}
	client := login(t, startGateway(t, server))

	output, err := client.Execute(testContext(t), `say "a; b"`)
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if !strings.Contains(output, "cannot be sent over RCON") {
		t.Fatalf("output = %q, want a refusal", output)
	}
	if got := server.received(); len(got) != 0 {
		t.Fatalf("forwarded %q, want nothing", got)
	}
}

func TestOverlongCommandIsRefused(t *testing.T) {
	server := &fakeServer{}
	client := login(t, startGateway(t, server))

	output, err := client.Execute(testContext(t), "say "+strings.Repeat("a", 600))
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if !strings.Contains(output, "too long") || len(server.received()) != 0 {
		t.Fatalf("output = %q, forwarded %q", output, server.received())
	}
}

func TestStatusAddressesAreMasked(t *testing.T) {
	// A command run over RCON prints its output after the plugin's marker.
	status := "hostname: Test 1.2.3\n" +
		"version : 11191711/24 11191711 secure\n" +
		"udp/ip  : 169.254.10.20:41234  (local: 0.0.0.0:27015)  (public IP from Steam: 198.51.100.5)\n" +
		"sourcetv:  198.51.100.5:27020, delay 90.0s  (local: 10.0.0.2:27020)\n" +
		"# userid name                uniqueid            connected ping loss state  adr\n" +
		"#      2 \"SourceTV\"          BOT                                     active\n" +
		"#      3 \"player 1.2.3.4:5\"  [U:1:12345]         01:02       40    0 active 203.0.113.9:27005\n"
	server := &fakeServer{reply: func(string) (string, error) {
		return resultMarker + "OK\n" + status, nil
	}}
	client := login(t, startGateway(t, server))

	output, err := client.Execute(testContext(t), "status")
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	for _, leaked := range []string{"198.51.100.5", "169.254.10.20", "10.0.0.2", "203.0.113.9", "1.2.3.4"} {
		if strings.Contains(output, leaked) {
			t.Fatalf("output shows %s:\n%s", leaked, output)
		}
	}
	for _, want := range []string{
		"udp/ip  : 0.0.0.0:41234  (local: 0.0.0.0:27015)  (public IP from Steam: 0.0.0.0)\n",
		"active 0.0.0.0:27005\n",
		"version : 11191711/24 11191711 secure\n",
		"hostname: Test 1.2.3\n",
	} {
		if !strings.Contains(output, want) {
			t.Fatalf("output lacks %q:\n%s", want, output)
		}
	}
}

func TestOtherOutputKeepsAddresses(t *testing.T) {
	server := &fakeServer{reply: func(string) (string, error) {
		return resultMarker + "OK\nlogaddress_list: 1 entry\n203.0.113.5:27500\n", nil
	}}
	client := login(t, startGateway(t, server))

	output, err := client.Execute(testContext(t), "logaddress_list")
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if !strings.Contains(output, "203.0.113.5:27500") {
		t.Fatalf("a log receiver was masked:\n%s", output)
	}
}

func TestLongOutputSpansSeveralPackets(t *testing.T) {
	long := strings.Repeat("0123456789abcdef", 1000) + "\n"
	server := &fakeServer{reply: func(string) (string, error) {
		return resultMarker + "OK\n" + long, nil
	}}
	client := login(t, startGateway(t, server))

	output, err := client.Execute(testContext(t), "mp_timelimit")
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if output != long {
		t.Fatalf("got %d bytes, want %d", len(output), len(long))
	}
}

func TestServerWithoutThePluginInterface(t *testing.T) {
	server := &fakeServer{reply: func(string) (string, error) {
		return "Unknown command \"sm_summon_rcon_command\"\n", nil
	}}
	client := login(t, startGateway(t, server))

	output, err := client.Execute(testContext(t), "status")
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if output != messageUnavailable+"\n" {
		t.Fatalf("output = %q", output)
	}
}

func TestWrongPasswordIsRejected(t *testing.T) {
	server := &fakeServer{}
	gateway := startGateway(t, server)

	for _, password := range []string{"", "wrong", testPassword + "x", testPassword[:len(testPassword)-1]} {
		client := dial(t, gateway)
		if err := client.Authenticate(testContext(t), password); !errors.Is(err, rcon.ErrAuthFailed) {
			t.Fatalf("Authenticate(%q) = %v, want ErrAuthFailed", password, err)
		}
	}
	if got := server.received(); len(got) != 0 {
		t.Fatalf("forwarded %q before any login", got)
	}
}

func TestRepeatedFailuresBlockTheAddressForAWhile(t *testing.T) {
	gateway := startGateway(t, &fakeServer{})
	now := time.Now()
	var clockMu sync.Mutex
	gateway.failures.now = func() time.Time {
		clockMu.Lock()
		defer clockMu.Unlock()
		return now
	}

	for attempt := 0; attempt < maxAuthFailures; attempt++ {
		client := dial(t, gateway)
		if err := client.Authenticate(testContext(t), "wrong"); !errors.Is(err, rcon.ErrAuthFailed) {
			t.Fatalf("attempt %d = %v, want ErrAuthFailed", attempt, err)
		}
	}

	// The right password is refused during the block, and trying it does not
	// extend the block.
	for attempt := 0; attempt < 3; attempt++ {
		client := dial(t, gateway)
		if err := client.Authenticate(testContext(t), testPassword); !errors.Is(err, rcon.ErrAuthFailed) {
			t.Fatalf("blocked login = %v, want ErrAuthFailed", err)
		}
	}

	clockMu.Lock()
	now = now.Add(blockDuration + time.Second)
	clockMu.Unlock()
	client := dial(t, gateway)
	if err := client.Authenticate(testContext(t), testPassword); err != nil {
		t.Fatalf("login after the block = %v", err)
	}
}

func TestCommandBeforeLoginIsRefused(t *testing.T) {
	server := &fakeServer{}
	gateway := startGateway(t, server)
	conn, err := net.Dial("tcp4", gateway.Addr().String())
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()

	if err := rcon.WritePacket(conn, rcon.Packet{ID: 7, Type: rcon.TypeExecCommand, Body: []byte("status")}); err != nil {
		t.Fatalf("write: %v", err)
	}
	_ = conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	packet, err := rcon.ReadPacket(conn, 8192)
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	if packet.Type != rcon.TypeAuthResponse || packet.ID != -1 {
		t.Fatalf("got %+v, want an authentication failure", packet)
	}
	if _, err := rcon.ReadPacket(conn, 8192); !errors.Is(err, io.EOF) {
		t.Fatalf("connection stayed open: %v", err)
	}
	if got := server.received(); len(got) != 0 {
		t.Fatalf("forwarded %q before login", got)
	}
}

func TestUnreachableGameServerDropsTheClient(t *testing.T) {
	server := &fakeServer{reply: func(string) (string, error) {
		return "", errors.New("game server unreachable")
	}}
	client := login(t, startGateway(t, server))

	if _, err := client.Execute(testContext(t), "status"); err == nil {
		t.Fatal("Execute succeeded although the game server was unreachable")
	}
}

func TestGatewayRejectsWeakConfiguration(t *testing.T) {
	base := Config{Addr: "127.0.0.1:0", Password: testPassword, ActorSteamID: testActor, Upstream: &fakeServer{}}
	cases := map[string]func(*Config){
		"short password":   func(c *Config) { c.Password = "short" },
		"password newline": func(c *Config) { c.Password = "long-enough-pass\nword" },
		"bad steam id":     func(c *Config) { c.ActorSteamID = "76561198000000000; quit" },
		"no upstream":      func(c *Config) { c.Upstream = nil },
	}
	for name, mutate := range cases {
		config := base
		mutate(&config)
		if gateway, err := Listen(config); err == nil {
			gateway.Close()
			t.Fatalf("%s: Listen succeeded", name)
		}
	}
}

func TestReplaceClosesThePreviousGatewayOnTheAddress(t *testing.T) {
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	addr := listener.Addr().String()
	_ = listener.Close()

	config := Config{Addr: addr, Password: testPassword, ActorSteamID: testActor, Upstream: &fakeServer{}}
	first, err := Replace(config)
	if err != nil {
		t.Fatalf("first Replace: %v", err)
	}
	second, err := Replace(config)
	if err != nil {
		t.Fatalf("second Replace: %v", err)
	}
	defer second.Close()
	select {
	case <-first.Done():
	default:
		t.Fatal("the first gateway is still open")
	}
}

func TestSplitCommands(t *testing.T) {
	cases := map[string][]string{
		"":                       nil,
		"  status  ":             {"status"},
		"a;b":                    {"a", "b"},
		"a ; ; b ;":              {"a", "b"},
		`say "a;b"`:              {`say "a;b"`},
		`say "a;b" ; c`:          {`say "a;b"`, "c"},
		"say \"open\nquit\"":     {`say "open`, `quit"`},
		`say "open ; a` + "\n;b": {`say "open ; a`, "b"},
		"a\x00;b":                {"a"},
	}
	for request, want := range cases {
		got := splitCommands(request)
		if len(got) != len(want) {
			t.Fatalf("splitCommands(%q) = %q, want %q", request, got, want)
		}
		for index := range want {
			if got[index] != want[index] {
				t.Fatalf("splitCommands(%q) = %q, want %q", request, got, want)
			}
		}
	}
}

func TestParsePluginResult(t *testing.T) {
	cases := []struct {
		output string
		body   string
		ok     bool
	}{
		{resultMarker + "OK\n", "", true},
		{resultMarker + "OK\r\nline one\r\nline two\r\n", "line one\nline two\n", true},
		{"preamble\n" + resultMarker + "OK\nbody", "body\n", true},
		{resultMarker + "ERROR cooldown\nPlease wait.\n", "Please wait.\n", true},
		{resultMarker + "OK :  spoofed by a chat line\n", "", false},
		{"Unknown command\n", "", false},
		{"", "", false},
	}
	for _, testCase := range cases {
		body, ok := parsePluginResult(testCase.output)
		if body != testCase.body || ok != testCase.ok {
			t.Fatalf("parsePluginResult(%q) = %q, %v; want %q, %v",
				testCase.output, body, ok, testCase.body, testCase.ok)
		}
	}
}
