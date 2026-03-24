// Package sdr provides SDR (Steam Datagram Relay) FakeIP detection.
// It parses the TF2 server's status output to extract SDR addresses.
package sdr

import (
	"regexp"
	"strconv"
	"strings"
)

// ServerInfo contains parsed server information including SDR FakeIP.
type ServerInfo struct {
	// Real server addresses (local network)
	RealIP     string
	RealPort   int
	RealTVPort int

	// SDR FakeIP addresses (what players connect to)
	SDRIP     string
	SDRPort   int
	SDRTVPort int

	// Additional server info
	Hostname    string
	Map         string
	PlayerCount int
	MaxPlayers  int
	BotCount    int
}

// ParseStatus parses the output of the TF2 "status" command and extracts
// SDR FakeIP information. SDR FakeIPs use the 169.254.x.x range.
//
// Example status output:
//
//	hostname: TF2 Server | Los Angeles #1
//	version : 2000/24 2000 secure
//	udp/ip  : 169.254.214.222:35344  (local: 198.51.100.5:27015)  (public IP from Steam: 198.51.100.5)
//	steamid : [G:1:1000001] (90000000000000001)
//	map     : pl_goldrush at: 0 x, 0 y, 0 z
//	tags    : alltalk,payload,sdr,steamnetworking
//	sourcetv:  169.254.214.222:35344, delay 0.0s  (local: 198.51.100.5:27020)
//	players : 24 humans, 1 bots (25 max)
func ParseStatus(statusOutput string) *ServerInfo {
	info := &ServerInfo{}

	lines := strings.Split(statusOutput, "\n")
	for _, line := range lines {
		line = strings.TrimSpace(line)

		// Parse hostname
		if strings.HasPrefix(line, "hostname:") {
			info.Hostname = strings.TrimSpace(strings.TrimPrefix(line, "hostname:"))
			continue
		}

		// Parse map
		if strings.HasPrefix(line, "map") {
			info.Map = parseMap(line)
			continue
		}

		// Parse udp/ip line for game server address
		// Format: udp/ip  : 169.254.214.222:35344  (local: 198.51.100.5:27015)
		if strings.HasPrefix(line, "udp/ip") {
			parseUDPIP(line, info)
			continue
		}

		// Parse sourcetv line for STV address
		// Format: sourcetv:  169.254.214.222:35344, delay 0.0s  (local: 198.51.100.5:27020)
		if strings.HasPrefix(line, "sourcetv:") {
			parseSourceTV(line, info)
			continue
		}

		// Parse players line
		// Format: players : 24 humans, 1 bots (25 max)
		if strings.HasPrefix(line, "players") {
			parsePlayersLine(line, info)
			continue
		}
	}

	return info
}

// parseMap extracts the map name from a line like "map     : pl_goldrush at: 0 x, 0 y, 0 z"
func parseMap(line string) string {
	// Remove "map" prefix and trim
	line = strings.TrimPrefix(line, "map")
	line = strings.TrimSpace(line)
	line = strings.TrimPrefix(line, ":")
	line = strings.TrimSpace(line)

	// Map name is before " at:"
	if idx := strings.Index(line, " at:"); idx > 0 {
		return line[:idx]
	}
	return line
}

// parseUDPIP extracts both SDR FakeIP and real IP from the udp/ip line.
// Format: udp/ip  : 169.254.214.222:35344  (local: 198.51.100.5:27015)
func parseUDPIP(line string, info *ServerInfo) {
	// Match the first IP:port (SDR FakeIP if present)
	reFirst := regexp.MustCompile(`udp/ip\s+:\s+(\d+\.\d+\.\d+\.\d+):(\d+)`)
	if matches := reFirst.FindStringSubmatch(line); len(matches) >= 3 {
		ip := matches[1]
		port, _ := strconv.Atoi(matches[2])

		// Check if it's an SDR FakeIP (169.254.x.x range)
		if strings.HasPrefix(ip, "169.254.") {
			info.SDRIP = ip
			info.SDRPort = port
		} else {
			// No SDR, this is the real IP
			info.RealIP = ip
			info.RealPort = port
		}
	}

	// Match the local IP:port in parentheses
	reLocal := regexp.MustCompile(`\(local:\s*(\d+\.\d+\.\d+\.\d+):(\d+)\)`)
	if matches := reLocal.FindStringSubmatch(line); len(matches) >= 3 {
		info.RealIP = matches[1]
		info.RealPort, _ = strconv.Atoi(matches[2])
	}
}

// parseSourceTV extracts STV addresses from the sourcetv line.
// Format: sourcetv:  169.254.214.222:35344, delay 0.0s  (local: 198.51.100.5:27020)
func parseSourceTV(line string, info *ServerInfo) {
	// Match the first IP:port (SDR FakeIP for STV if present)
	reFirst := regexp.MustCompile(`sourcetv:\s+(\d+\.\d+\.\d+\.\d+):(\d+)`)
	if matches := reFirst.FindStringSubmatch(line); len(matches) >= 3 {
		ip := matches[1]
		port, _ := strconv.Atoi(matches[2])

		// Check if it's an SDR FakeIP (169.254.x.x range)
		if strings.HasPrefix(ip, "169.254.") {
			info.SDRTVPort = port
		} else {
			info.RealTVPort = port
		}
	}

	// Match the local STV port in parentheses
	reLocal := regexp.MustCompile(`\(local:\s*(\d+\.\d+\.\d+\.\d+):(\d+)\)`)
	if matches := reLocal.FindStringSubmatch(line); len(matches) >= 3 {
		info.RealTVPort, _ = strconv.Atoi(matches[2])
	}
}

// parsePlayersLine extracts player counts from the players line.
// Format: players : 24 humans, 1 bots (25 max)
func parsePlayersLine(line string, info *ServerInfo) {
	// Match human count
	reHumans := regexp.MustCompile(`(\d+)\s+humans?`)
	if matches := reHumans.FindStringSubmatch(line); len(matches) >= 2 {
		info.PlayerCount, _ = strconv.Atoi(matches[1])
	}

	// Match bot count
	reBots := regexp.MustCompile(`(\d+)\s+bots?`)
	if matches := reBots.FindStringSubmatch(line); len(matches) >= 2 {
		info.BotCount, _ = strconv.Atoi(matches[1])
	}

	// Match max players
	reMax := regexp.MustCompile(`\((\d+)\s+max\)`)
	if matches := reMax.FindStringSubmatch(line); len(matches) >= 2 {
		info.MaxPlayers, _ = strconv.Atoi(matches[1])
	}
}

// HasSDR returns true if the server is using SDR (FakeIP detected).
func (s *ServerInfo) HasSDR() bool {
	return s.SDRIP != "" && strings.HasPrefix(s.SDRIP, "169.254.")
}

// GetConnectAddress returns the address players should connect to.
// Returns SDR FakeIP if available, otherwise the real IP.
func (s *ServerInfo) GetConnectAddress() (ip string, port int) {
	if s.HasSDR() {
		return s.SDRIP, s.SDRPort
	}
	return s.RealIP, s.RealPort
}

// GetSTVAddress returns the SourceTV address for spectators.
// Returns SDR FakeIP STV port if available, otherwise the real STV port.
func (s *ServerInfo) GetSTVAddress() (ip string, port int) {
	if s.HasSDR() {
		return s.SDRIP, s.SDRTVPort
	}
	return s.RealIP, s.RealTVPort
}
