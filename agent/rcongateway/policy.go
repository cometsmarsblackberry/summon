package rcongateway

import (
	"regexp"
	"strings"
)

const (
	// pluginCommand is the Summon plugin's entry point for one command from
	// a remote RCON client. The plugin holds the allowlist and runs the
	// command, so the gateway never sends anything else to the game server.
	pluginCommand = "sm_summon_rcon_command"
	// resultMarker starts the line the plugin prints before a command's
	// output, followed by OK or ERROR and an error code.
	resultMarker = "SUMMON_OWNER_COMMAND_RESULT "
	// maxForwardedLine keeps a forwarded line under the 512 bytes the engine
	// accepts for one console command.
	maxForwardedLine = 500
	// maxCommandsPerRequest bounds how many commands one packet may carry.
	maxCommandsPerRequest = 32

	messageUnsupported = "This command cannot be sent over RCON: semicolons inside quotes and control characters are not supported."
	messageTooLong     = "This command is too long to send over RCON."
	messageTooMany     = "Too many commands in one RCON request."
	messageUnavailable = "Restricted RCON is not available on this server."
)

// splitCommands separates the commands in one RCON request the way the
// engine's command buffer does: at line breaks, and at semicolons outside
// double quotes. Quoting starts afresh with every command. It may split in
// more places than the engine would, never in fewer.
func splitCommands(request string) []string {
	// The engine reads the request as a C string.
	if end := strings.IndexByte(request, 0); end >= 0 {
		request = request[:end]
	}

	var commands []string
	start := 0
	inQuotes := false
	flush := func(end int) {
		if command := strings.TrimSpace(request[start:end]); command != "" {
			commands = append(commands, command)
		}
		start = end + 1
		inQuotes = false
	}
	for index := 0; index < len(request); index++ {
		switch request[index] {
		case '"':
			inQuotes = !inQuotes
		case ';':
			if !inQuotes {
				flush(index)
			}
		case '\n', '\r':
			flush(index)
		}
	}
	flush(len(request))
	return commands
}

// forwardable reports whether a command can be sent to the game server as
// part of one console line. The engine would split that line at a semicolon
// outside quotes or at a line break, and run what follows without the
// plugin's checks, so no forwarded command contains either one, quoted or
// not. This check is the gateway's security boundary; splitCommands is not.
func forwardable(command string) bool {
	for index := 0; index < len(command); index++ {
		character := command[index]
		if character == ';' || character < 0x20 || character == 0x7f {
			return false
		}
	}
	return command != ""
}

// wrapCommand builds the console line that asks the plugin to run command
// for the reservation owner.
func wrapCommand(actorSteamID, command string) string {
	return pluginCommand + " " + actorSteamID + " " + command
}

// parsePluginResult extracts a command's output from what the game server
// printed. ok is false when the plugin's marker is missing, which means the
// server does not have the restricted command interface.
//
// Log lines printed while the command was being checked can precede the
// marker. For a refused command the output is the plugin's explanation.
func parsePluginResult(output string) (body string, ok bool) {
	normalized := strings.ReplaceAll(output, "\r\n", "\n")
	lines := strings.Split(normalized, "\n")
	for index, line := range lines {
		if !strings.HasPrefix(line, resultMarker) {
			continue
		}
		status := strings.TrimSpace(line[len(resultMarker):])
		if status != "OK" && !strings.HasPrefix(status, "ERROR ") {
			return "", false
		}
		body = strings.Join(lines[index+1:], "\n")
		body = strings.TrimLeft(body, "\n")
		if body != "" && !strings.HasSuffix(body, "\n") {
			body += "\n"
		}
		return body, true
	}
	return "", false
}

var ipv4RE = regexp.MustCompile(`\b[0-9]{1,3}(?:\.[0-9]{1,3}){3}\b`)

// maskedIPv4 stands in for every address in "status" output. The port stays.
const maskedIPv4 = "0.0.0.0"

// maskStatusAddresses replaces every IPv4 address in "status" output: each
// player's, and the server's own, which owners do not otherwise see when
// players connect through Steam's relays. It goes by the output, not by the
// command's name, so no spelling of the command skips it.
//
// The plugin cannot do this for RCON: a command run over RCON prints its
// output after the plugin has returned.
func maskStatusAddresses(output string) string {
	for _, line := range strings.Split(output, "\n") {
		if strings.HasPrefix(line, "udp/ip") || strings.HasPrefix(line, "# userid") {
			return ipv4RE.ReplaceAllString(output, maskedIPv4)
		}
	}
	return output
}
