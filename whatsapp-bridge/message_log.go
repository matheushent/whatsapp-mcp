package main

import "fmt"

// formatMessageLogLine renders the console line for a stored message. It
// deliberately omits the message text: the bridge's stdout is routinely
// redirected to a log file (launchd, docker, systemd), and chat contents
// copied there outlive the session and may include secrets people paste into
// chats. The length is enough to follow activity; read the text through the MCP
// tools or messages.db. The /api/send handler already follows the same rule.
func formatMessageLogLine(timestamp, direction, sender, mediaType, filename, content string) string {
	if mediaType != "" {
		return fmt.Sprintf("[%s] %s %s: [%s: %s] (%d chars)", timestamp, direction, sender, mediaType, filename, len([]rune(content)))
	}
	if content != "" {
		return fmt.Sprintf("[%s] %s %s: (%d chars)", timestamp, direction, sender, len([]rune(content)))
	}
	return ""
}
