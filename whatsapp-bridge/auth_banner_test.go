package main

import (
	"bytes"
	"strings"
	"testing"
)

const bannerTestToken = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

func TestWriteTokenBannerShowsTokenOnTerminal(t *testing.T) {
	var buf bytes.Buffer
	writeTokenBanner(&buf, bannerTestToken, 8080, true)
	out := buf.String()
	if !strings.Contains(out, bannerTestToken) {
		t.Fatal("interactive banner must show the token so the user can copy it")
	}
	if !strings.Contains(out, "http://127.0.0.1:8080/api") || !strings.Contains(out, tokenFilePath) {
		t.Errorf("banner lost its URL or token path: %q", out)
	}
}

// When stdout is a log file (launchd, docker), the secret must not be written
// to it; the banner points at the 0600 token file instead.
func TestWriteTokenBannerOmitsTokenWhenNotATerminal(t *testing.T) {
	var buf bytes.Buffer
	writeTokenBanner(&buf, bannerTestToken, 8080, false)
	out := buf.String()
	if strings.Contains(out, bannerTestToken) {
		t.Fatal("banner leaked the token into non-terminal output")
	}
	if !strings.Contains(out, tokenFilePath) || !strings.Contains(out, "not shown") {
		t.Errorf("non-terminal banner should say where to read the token: %q", out)
	}
}
