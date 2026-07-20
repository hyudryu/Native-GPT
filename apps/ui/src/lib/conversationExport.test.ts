import { describe, expect, it } from "vitest";
import { conversationMarkdown, safeExportName } from "./conversationExport";
import type { Conversation, Message } from "./dataApi";

describe("conversation export", () => {
  it("creates readable Markdown", () => {
    const conversation = { id: "c1", title: "Test chat" } satisfies Conversation;
    const messages = [
      { id: "m1", conversation_id: "c1", role: "user", content: "Hello" },
      { id: "m2", conversation_id: "c1", role: "assistant", content: "Hi" },
    ] satisfies Message[];
    expect(conversationMarkdown(conversation, messages)).toContain(
      "# Test chat\n\n## User\n\nHello\n\n## Assistant\n\nHi",
    );
  });

  it("makes a Windows-safe filename", () => {
    expect(safeExportName('  My: chat/notes?  ')).toBe("My- chat-notes-");
    expect(safeExportName("   ")).toBe("conversation");
  });
});
