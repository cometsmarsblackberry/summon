package sdr

import (
	"testing"
)

// Sample status output from a real TF2 server with SDR enabled
const sampleStatusOutput = `hostname: TF2 Server | Los Angeles #1
version : 2000/24 2000 secure
udp/ip  : 169.254.214.222:35344  (local: 198.51.100.5:27015)  (public IP from Steam: 198.51.100.5)
steamid : [G:1:1000001] (90000000000000001)
map     : pl_goldrush at: 0 x, 0 y, 0 z
tags    : alltalk,payload,sdr,steamnetworking
sourcetv:  169.254.214.222:35344, delay 0.0s  (local: 198.51.100.5:27020)
players : 24 humans, 1 bots (25 max)
edicts  : 1296 used of 2048 max
# userid name                uniqueid            connected ping loss state  adr
#      2 "SourceTV"          BOT                                     active
#     98 "John Doe"          [U:1:100000001]      1:11:28    39    0 active 169.254.249.174:9811`

// Sample status output without SDR (direct connection)
const noSDRStatusOutput = `hostname: Test Server
version : 2000/24 2000 secure
udp/ip  : 192.168.1.100:27015
steamid : [G:1:1000001] (90000000000000001)
map     : cp_badlands at: 0 x, 0 y, 0 z
sourcetv:  192.168.1.100:27020, delay 0.0s
players : 12 humans, 0 bots (24 max)`

func TestParseStatus_WithSDR(t *testing.T) {
	info := ParseStatus(sampleStatusOutput)

	// Check SDR addresses
	if info.SDRIP != "169.254.214.222" {
		t.Errorf("SDRIP = %q, want %q", info.SDRIP, "169.254.214.222")
	}
	if info.SDRPort != 35344 {
		t.Errorf("SDRPort = %d, want %d", info.SDRPort, 35344)
	}
	if info.SDRTVPort != 35344 {
		t.Errorf("SDRTVPort = %d, want %d", info.SDRTVPort, 35344)
	}

	// Check real addresses
	if info.RealIP != "198.51.100.5" {
		t.Errorf("RealIP = %q, want %q", info.RealIP, "198.51.100.5")
	}
	if info.RealPort != 27015 {
		t.Errorf("RealPort = %d, want %d", info.RealPort, 27015)
	}
	if info.RealTVPort != 27020 {
		t.Errorf("RealTVPort = %d, want %d", info.RealTVPort, 27020)
	}

	// Check other info
	if info.Hostname != "TF2 Server | Los Angeles #1" {
		t.Errorf("Hostname = %q, want %q", info.Hostname, "TF2 Server | Los Angeles #1")
	}
	if info.Map != "pl_goldrush" {
		t.Errorf("Map = %q, want %q", info.Map, "pl_goldrush")
	}
	if info.PlayerCount != 24 {
		t.Errorf("PlayerCount = %d, want %d", info.PlayerCount, 24)
	}
	if info.BotCount != 1 {
		t.Errorf("BotCount = %d, want %d", info.BotCount, 1)
	}
	if info.MaxPlayers != 25 {
		t.Errorf("MaxPlayers = %d, want %d", info.MaxPlayers, 25)
	}

	// Check HasSDR
	if !info.HasSDR() {
		t.Error("HasSDR() = false, want true")
	}

	// Check GetConnectAddress returns SDR address
	ip, port := info.GetConnectAddress()
	if ip != "169.254.214.222" || port != 35344 {
		t.Errorf("GetConnectAddress() = (%q, %d), want (%q, %d)", ip, port, "169.254.214.222", 35344)
	}
}

func TestParseStatus_WithoutSDR(t *testing.T) {
	info := ParseStatus(noSDRStatusOutput)

	// Check no SDR addresses
	if info.SDRIP != "" {
		t.Errorf("SDRIP = %q, want empty", info.SDRIP)
	}
	if info.SDRPort != 0 {
		t.Errorf("SDRPort = %d, want 0", info.SDRPort)
	}

	// Check real addresses
	if info.RealIP != "192.168.1.100" {
		t.Errorf("RealIP = %q, want %q", info.RealIP, "192.168.1.100")
	}
	if info.RealPort != 27015 {
		t.Errorf("RealPort = %d, want %d", info.RealPort, 27015)
	}

	// Check other info
	if info.Map != "cp_badlands" {
		t.Errorf("Map = %q, want %q", info.Map, "cp_badlands")
	}
	if info.PlayerCount != 12 {
		t.Errorf("PlayerCount = %d, want %d", info.PlayerCount, 12)
	}
	if info.BotCount != 0 {
		t.Errorf("BotCount = %d, want %d", info.BotCount, 0)
	}
	if info.MaxPlayers != 24 {
		t.Errorf("MaxPlayers = %d, want %d", info.MaxPlayers, 24)
	}

	// Check HasSDR
	if info.HasSDR() {
		t.Error("HasSDR() = true, want false")
	}

	// Check GetConnectAddress returns real address when no SDR
	ip, port := info.GetConnectAddress()
	if ip != "192.168.1.100" || port != 27015 {
		t.Errorf("GetConnectAddress() = (%q, %d), want (%q, %d)", ip, port, "192.168.1.100", 27015)
	}
}

func TestParseStatus_EdgeCases(t *testing.T) {
	// Test empty input
	info := ParseStatus("")
	if info.HasSDR() {
		t.Error("HasSDR() on empty input = true, want false")
	}

	// Test partial input
	partialOutput := `hostname: Test Server
map     : ctf_2fort at: 0 x, 0 y, 0 z
players : 5 humans, 2 bots (32 max)`
	info = ParseStatus(partialOutput)
	if info.Map != "ctf_2fort" {
		t.Errorf("Map = %q, want %q", info.Map, "ctf_2fort")
	}
	if info.PlayerCount != 5 {
		t.Errorf("PlayerCount = %d, want %d", info.PlayerCount, 5)
	}
}

func TestParseMap(t *testing.T) {
	tests := []struct {
		line     string
		expected string
	}{
		{"map     : pl_goldrush at: 0 x, 0 y, 0 z", "pl_goldrush"},
		{"map     : cp_badlands at: 0 x, 0 y, 0 z", "cp_badlands"},
		{"map     : ctf_2fort at: -1024 x, 512 y, 0 z", "ctf_2fort"},
	}

	for _, test := range tests {
		result := parseMap(test.line)
		if result != test.expected {
			t.Errorf("parseMap(%q) = %q, want %q", test.line, result, test.expected)
		}
	}
}
