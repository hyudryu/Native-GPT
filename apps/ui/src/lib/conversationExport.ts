import { messageText, type Conversation, type Message } from "./dataApi";

export function safeExportName(title: string): string {
  const name = title
    .trim()
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, "-")
    .replace(/\s+/g, " ")
    .slice(0, 80);
  return name || "conversation";
}

export function conversationMarkdown(
  conversation: Conversation,
  messages: Message[],
): string {
  const lines = [`# ${conversation.title}`, ""];
  for (const message of messages) {
    const label = message.role.charAt(0).toUpperCase() + message.role.slice(1);
    lines.push(`## ${label}`, "", messageText(message.content ?? message.content_json), "");
  }
  return lines.join("\n").trimEnd() + "\n";
}
