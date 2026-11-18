package rcon

import (
	"bytes"
	"encoding/binary"
	"errors"
	"testing"
)

func frame(id, packetType int32, rest []byte) []byte {
	payload := binary.LittleEndian.AppendUint32(nil, uint32(id))
	payload = binary.LittleEndian.AppendUint32(payload, uint32(packetType))
	payload = append(payload, rest...)
	return append(binary.LittleEndian.AppendUint32(nil, uint32(len(payload))), payload...)
}

func TestReadRequestEndsTheBodyAtTheFirstNUL(t *testing.T) {
	for _, test := range []struct {
		name string
		rest string
		want string
	}{
		{"two terminators", "status\x00\x00", "status"},
		{"four terminators", "status\x00\x00\x00\x00", "status"},
		{"one terminator", "status\x00", "status"},
		{"empty body", "\x00\x00", ""},
		{"empty body, one terminator", "\x00", ""},
	} {
		t.Run(test.name, func(t *testing.T) {
			packet, err := ReadRequest(bytes.NewReader(frame(7, TypeExecCommand, []byte(test.rest))), 4096)
			if err != nil {
				t.Fatalf("ReadRequest: %v", err)
			}
			if packet.ID != 7 || packet.Type != TypeExecCommand || string(packet.Body) != test.want {
				t.Fatalf("got %+v, want ID 7, type %d, body %q", packet, TypeExecCommand, test.want)
			}
		})
	}
}

func TestReadRequestRejectsBadSizes(t *testing.T) {
	for _, data := range [][]byte{
		frame(7, TypeAuth, nil),
		frame(7, TypeAuth, bytes.Repeat([]byte("a"), 64)),
	} {
		if _, err := ReadRequest(bytes.NewReader(data), 32); !errors.Is(err, ErrPacketSize) {
			t.Fatalf("ReadRequest(%d bytes) = %v, want ErrPacketSize", len(data), err)
		}
	}
}

// ReadPacket reads what SRCDS sends, whose bodies can hold NULs: its reply to
// an empty response value is 00 01 00 00.
func TestReadPacketKeepsTheWholeBody(t *testing.T) {
	packet, err := ReadPacket(bytes.NewReader(frame(7, TypeResponseValue, []byte("\x00\x01\x00\x00\x00\x00"))), 4096)
	if err != nil {
		t.Fatalf("ReadPacket: %v", err)
	}
	if want := []byte{0x00, 0x01, 0x00, 0x00}; !bytes.Equal(packet.Body, want) {
		t.Fatalf("body = %x, want %x", packet.Body, want)
	}
}
