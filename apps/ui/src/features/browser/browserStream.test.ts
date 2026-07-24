import { describe, expect, it } from "vitest";
import { browserStreamUrl, parseFrame } from "./browserStream";
import { FRAME_HEADER_LEN, FRAME_VERSION } from "./types";

/** Build a binary frame exactly as the server encodes it (big-endian). */
function encodeFrame(options: {
  frameId: bigint;
  width: number;
  height: number;
  format: number;
  image: number[];
  version?: number;
}): ArrayBuffer {
  const buffer = new ArrayBuffer(FRAME_HEADER_LEN + options.image.length);
  const view = new DataView(buffer);
  view.setUint8(0, options.version ?? FRAME_VERSION);
  view.setBigUint64(1, options.frameId, false);
  view.setUint32(9, options.width, false);
  view.setUint32(13, options.height, false);
  view.setUint8(17, options.format);
  new Uint8Array(buffer, FRAME_HEADER_LEN).set(options.image);
  return buffer;
}

describe("parseFrame", () => {
  it("decodes a jpeg frame with big-endian header fields", () => {
    const buffer = encodeFrame({
      frameId: 0x0102030405060708n,
      width: 1280,
      height: 720,
      format: 1,
      image: [0xff, 0xd8, 0xff, 0xe0, 1, 2, 3],
    });
    const frame = parseFrame(buffer);
    expect(frame).not.toBeNull();
    expect(frame!.frameId).toBe(Number(0x0102030405060708n));
    expect(frame!.width).toBe(1280);
    expect(frame!.height).toBe(720);
    expect(frame!.format).toBe("jpeg");
    expect(frame!.mime).toBe("image/jpeg");
    expect(Array.from(frame!.image)).toEqual([0xff, 0xd8, 0xff, 0xe0, 1, 2, 3]);
  });

  it("decodes a webp frame", () => {
    const buffer = encodeFrame({
      frameId: 42n,
      width: 640,
      height: 480,
      format: 2,
      image: [0x52, 0x49, 0x46, 0x46],
    });
    const frame = parseFrame(buffer);
    expect(frame!.format).toBe("webp");
    expect(frame!.mime).toBe("image/webp");
    expect(frame!.frameId).toBe(42);
  });

  it("rejects short buffers", () => {
    expect(parseFrame(new ArrayBuffer(3))).toBeNull();
    expect(parseFrame(new ArrayBuffer(FRAME_HEADER_LEN - 1))).toBeNull();
  });

  it("rejects an unsupported frame version", () => {
    const buffer = encodeFrame({
      frameId: 1n,
      width: 1,
      height: 1,
      format: 1,
      image: [],
      version: 9,
    });
    expect(parseFrame(buffer)).toBeNull();
  });

  it("rejects an unsupported format byte", () => {
    const buffer = encodeFrame({
      frameId: 1n,
      width: 1,
      height: 1,
      format: 77,
      image: [],
    });
    expect(parseFrame(buffer)).toBeNull();
  });

  it("accepts an empty image payload", () => {
    const buffer = encodeFrame({
      frameId: 7n,
      width: 0,
      height: 0,
      format: 1,
      image: [],
    });
    const frame = parseFrame(buffer);
    expect(frame).not.toBeNull();
    expect(frame!.image.byteLength).toBe(0);
  });
});

describe("browserStreamUrl", () => {
  it("builds a same-origin ws URL with the token query param", () => {
    expect(
      browserStreamUrl(
        { protocol: "http:", host: "localhost:8787" },
        "tok123",
      ),
    ).toBe("ws://localhost:8787/api/browser/stream?token=tok123");
  });

  it("upgrades to wss on https pages and encodes the token", () => {
    expect(
      browserStreamUrl(
        { protocol: "https:", host: "100.x.y.z:8787" },
        "to ken",
      ),
    ).toBe("wss://100.x.y.z:8787/api/browser/stream?token=to%20ken");
  });

  it("omits the query when there is no token", () => {
    expect(
      browserStreamUrl({ protocol: "http:", host: "localhost:8787" }, null),
    ).toBe("ws://localhost:8787/api/browser/stream");
  });
});
