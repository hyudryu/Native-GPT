import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import type { FC } from "react";

/**
 * Renders a message with markdown support.
 *
 * - Non-streaming / completed messages: full markdown rendering via `react-markdown`.
 * - Streaming messages: raw text (markdown may be incomplete mid-stream).
 *
 * User messages are rendered as plain text — no markdown parsing for user input.
 */
export const MarkdownMessage: FC<{
  content: string;
  streaming?: boolean;
  className?: string;
}> = ({ content, streaming = false, className }) => {
  if (streaming || !content) {
    return <>{content || "Thinking\u2026"}</>;
  }

  return (
    <div className={className}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
        {content}
      </ReactMarkdown>
    </div>
  );
};

/** Renders user messages as plain text (no markdown). */
export const PlainMessage: FC<{ content: string }> = ({ content }) => <>{content}</>;
