package main

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"strings"
	"time"
)

// maxMediaBase64Bytes is the maximum file size that will be base64-encoded and
// included in a webhook payload. Files larger than this limit are skipped to
// avoid excessive memory use and oversized HTTP requests.
const maxMediaBase64Bytes = 10 * 1024 * 1024 // 10 MB

// webhookClient is used for all outbound webhook POSTs. The 30-second timeout
// prevents a slow or unreachable endpoint from blocking message handling
// indefinitely. Redirects are never followed: WEBHOOK_URL is a single
// operator-configured endpoint, not a browsable URL, and following a 3xx
// would forward X-Bridge-Token to whatever host the redirect names — Go only
// strips Authorization/Cookie on cross-origin redirects, not custom headers.
var webhookClient = &http.Client{
	Timeout: 30 * time.Second,
	CheckRedirect: func(req *http.Request, via []*http.Request) error {
		return http.ErrUseLastResponse
	},
}

// webhookAuthToken is the shared bridge token attached as an
// "X-Bridge-Token" header to every outbound webhook POST. It is populated
// once at startup from loadOrCreateBridgeToken() (see auth.go and main.go) —
// the same token the bridge already requires on inbound /api/* requests.
// When empty (no token configured yet) the header is omitted so deployments
// that predate the token rollout keep working. The receiving hub enforces
// this token on its inbound webhook route once its own WHATSAPP_BRIDGE_TOKEN
// is set to the matching value (autohub PR #898), which accepts the token via
// this header or "Authorization: Bearer". A dedicated header is used here
// (rather than Authorization) so it never collides with a receiver's own
// Authorization-based auth — e.g. HTTP Basic auth that net/http derives
// automatically from credentials embedded in WEBHOOK_URL.
var webhookAuthToken string

// WebhookPayload represents the data sent to the webhook
type WebhookPayload struct {
	EventType       string   `json:"eventType,omitempty"`
	Sender          string   `json:"sender"`
	Content         string   `json:"content"`
	ChatJID         string   `json:"chatJID"`
	IsFromMe        bool     `json:"isFromMe"`
	QuotedMessageId string   `json:"quotedMessageId,omitempty"`
	QuotedSender    string   `json:"quotedSender,omitempty"`
	QuotedContent   string   `json:"quotedContent,omitempty"`
	QuotedIsFromMe  *bool    `json:"quotedIsFromMe,omitempty"`
	MentionedJIDs   []string `json:"mentionedJids,omitempty"`
	// Media fields - populated when the message contains an image attachment
	MessageID     string `json:"messageId,omitempty"`
	MediaType     string `json:"mediaType,omitempty"`
	MimeType      string `json:"mimeType,omitempty"`
	MediaFilename string `json:"mediaFilename,omitempty"`
	MediaBase64   string `json:"mediaBase64,omitempty"`
	// Reaction fields - populated when EventType is "reaction".
	ReactionToMessageID string  `json:"reactionToMessageId,omitempty"`
	ReactionEmoji       *string `json:"reactionEmoji,omitempty"`
	ReactionRemoved     *bool   `json:"reactionRemoved,omitempty"`
}

// webhookURL returns the operator-configured WEBHOOK_URL, or "" when unset.
// There is deliberately no built-in fallback: webhooks carry every inbound
// message (text and image bytes) and the bridge token, so they only go to an
// endpoint the operator chose. A hardcoded local default let any local process
// that bound that port read all messages.
func webhookURL() string {
	return strings.TrimSpace(os.Getenv("WEBHOOK_URL"))
}

// webhooksEnabled reports whether webhook processing is enabled: WEBHOOK_URL
// must be set, and WEBHOOK_ENABLED must not be false (it defaults to true, so
// it is only needed to switch a configured URL off without unsetting it). Keep
// this separate from sendWebhookPayload so media callers can avoid their
// webhook-only file work when delivery is disabled.
func webhooksEnabled() bool {
	return webhookURL() != "" && getEnvBool("WEBHOOK_ENABLED", true)
}

// sendWebhookPayload marshals and POSTs a WebhookPayload to the configured webhook URL.
func sendWebhookPayload(payload WebhookPayload) {
	// No URL, or WEBHOOK_ENABLED=false: do nothing, with no connection attempt
	// and no per-message "connection refused" log noise.
	if !webhooksEnabled() {
		return
	}

	target := webhookURL()

	jsonData, err := json.Marshal(payload)
	if err != nil {
		fmt.Printf("Error marshaling webhook payload: %v\n", err)
		return
	}

	req, err := http.NewRequest(http.MethodPost, target, bytes.NewBuffer(jsonData))
	if err != nil {
		fmt.Printf("Error building webhook request: %v\n", err)
		return
	}
	req.Header.Set("Content-Type", "application/json")
	// Authenticate to the hub's fail-closed inbound webhook route with the
	// shared bridge token, via a dedicated header so it can never clobber a
	// receiver's own Authorization-based auth (see webhookAuthToken doc
	// comment above). WEBHOOK_URL is always operator-configured here (there is
	// no implicit default), so the token only ever goes where they pointed it.
	if webhookAuthToken != "" {
		req.Header.Set("X-Bridge-Token", webhookAuthToken)
	}

	resp, err := webhookClient.Do(req)
	if err != nil {
		fmt.Printf("Error sending webhook: %v\n", err)
		return
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode == 200 {
		fmt.Printf("✓ Webhook sent for message from %s\n", payload.Sender)
	} else {
		fmt.Printf("⚠ Webhook failed with status %d\n", resp.StatusCode)
	}
}

// SendWebhook sends a text-only message to the webhook endpoint. New callers
// should use SendWebhookWithMessageID so receiver-side idempotency can identify
// repeated WhatsApp events; this wrapper remains for compatibility.
func SendWebhook(sender, content, chatJID string, isFromMe bool, quotedMessageId, quotedSender, quotedContent string, quotedIsFromMe *bool, mentionedJIDs []string) {
	SendWebhookWithMessageID(sender, content, chatJID, isFromMe, quotedMessageId, quotedSender, quotedContent, quotedIsFromMe, mentionedJIDs, "")
}

// SendWebhookWithMessageID sends a text-only message and preserves the native
// WhatsApp message ID in the payload for downstream idempotency.
func SendWebhookWithMessageID(sender, content, chatJID string, isFromMe bool, quotedMessageId, quotedSender, quotedContent string, quotedIsFromMe *bool, mentionedJIDs []string, messageID string) {
	sendWebhookPayload(WebhookPayload{
		Sender:          sender,
		Content:         content,
		ChatJID:         chatJID,
		IsFromMe:        isFromMe,
		QuotedMessageId: quotedMessageId,
		QuotedSender:    quotedSender,
		QuotedContent:   quotedContent,
		QuotedIsFromMe:  quotedIsFromMe,
		MentionedJIDs:   mentionedJIDs,
		MessageID:       messageID,
	})
}

// SendWebhookWithMedia sends a message to the webhook endpoint including base64-encoded
// image data read from localPath. If localPath is empty or unreadable the webhook is
// still sent – just without the MediaBase64 field so the text caption is not lost.
func SendWebhookWithMedia(
	sender, content, chatJID string,
	isFromMe bool,
	quotedMessageId, quotedSender, quotedContent string,
	quotedIsFromMe *bool, mentionedJIDs []string,
	messageID, mediaType, mimeType, mediaFilename, localPath string,
) {
	if !webhooksEnabled() {
		return
	}

	var mediaBase64 string
	if localPath != "" {
		info, statErr := os.Stat(localPath)
		if statErr != nil {
			fmt.Printf("⚠ Could not stat media file for base64 encoding: %v\n", statErr)
		} else if info.Size() > maxMediaBase64Bytes {
			fmt.Printf("⚠ Media file too large for base64 encoding (%d bytes), skipping MediaBase64\n", info.Size())
		} else if data, err := os.ReadFile(localPath); err == nil {
			mediaBase64 = base64.StdEncoding.EncodeToString(data)
		} else {
			fmt.Printf("⚠ Could not read media file for base64 encoding: %v\n", err)
		}
	}

	sendWebhookPayload(WebhookPayload{
		Sender:          sender,
		Content:         content,
		ChatJID:         chatJID,
		IsFromMe:        isFromMe,
		QuotedMessageId: quotedMessageId,
		QuotedSender:    quotedSender,
		QuotedContent:   quotedContent,
		QuotedIsFromMe:  quotedIsFromMe,
		MentionedJIDs:   mentionedJIDs,
		MessageID:       messageID,
		MediaType:       mediaType,
		MimeType:        mimeType,
		MediaFilename:   mediaFilename,
		MediaBase64:     mediaBase64,
	})
}

// SendReactionWebhook sends a typed reaction event to the webhook endpoint.
func SendReactionWebhook(sender, chatJID string, isFromMe bool, messageID, reactionToMessageID, emoji string) {
	removed := emoji == ""
	sendWebhookPayload(WebhookPayload{
		EventType:           "reaction",
		Sender:              sender,
		Content:             emoji,
		ChatJID:             chatJID,
		IsFromMe:            isFromMe,
		MessageID:           messageID,
		MediaType:           "reaction",
		ReactionToMessageID: reactionToMessageID,
		ReactionEmoji:       &emoji,
		ReactionRemoved:     &removed,
	})
}

// In main.go, handleMessage forwards webhooks for messages with text content.
// It will forward self-sent messages when the env var FORWARD_SELF=true.
