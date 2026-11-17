package main

import (
	"strings"
	"testing"
)

func TestFormatMessageLogLineOmitsMessageText(t *testing.T) {
	const secret = "my password is hunter2"

	text := formatMessageLogLine("2026-11-17 10:00:00", "←", "12025551234", "", "", secret)
	if strings.Contains(text, "hunter2") || strings.Contains(text, secret) {
		t.Fatalf("text message log line leaks content: %q", text)
	}
	if want := "[2026-11-17 10:00:00] ← 12025551234: (22 chars)"; text != want {
		t.Errorf("text line = %q, want %q", text, want)
	}

	media := formatMessageLogLine("2026-11-17 10:00:00", "→", "12025551234", "image", "image_1.jpg", "caption with hunter2")
	if strings.Contains(media, "hunter2") {
		t.Fatalf("media message log line leaks caption: %q", media)
	}
	if !strings.Contains(media, "[image: image_1.jpg]") {
		t.Errorf("media line lost its type/filename: %q", media)
	}
}

func TestFormatMessageLogLineCountsRunesAndSkipsEmpty(t *testing.T) {
	if got := formatMessageLogLine("t", "←", "s", "", "", "héllo 👍"); !strings.HasSuffix(got, "(7 chars)") {
		t.Errorf("rune count wrong: %q", got)
	}
	if got := formatMessageLogLine("t", "←", "s", "", "", ""); got != "" {
		t.Errorf("empty text with no media should log nothing, got %q", got)
	}
}
