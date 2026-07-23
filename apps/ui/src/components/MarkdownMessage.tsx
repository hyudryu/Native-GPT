import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import {
  Children,
  isValidElement,
  useState,
  type FC,
  type ReactElement,
  type ReactNode,
} from "react";

/** Recursively extract plain text from rendered code children (for copying). */
function textOf(node: ReactNode): string {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) return textOf(node.props.children);
  return "";
}

/**
 * Fenced code block with a header bar: language label on the left, copy
 * button on the right. Syntax colors come from the hljs theme in
 * styles/markdown.css.
 */
const CodeBlock: FC<{ children?: ReactNode }> = ({ children }) => {
  const [copied, setCopied] = useState(false);
  const codeEl = Children.toArray(children).find(isValidElement) as
    | ReactElement<{ className?: string; children?: ReactNode }>
    | undefined;
  const lang = /language-([\w-]+)/.exec(codeEl?.props?.className ?? "")?.[1];

  const copy = async () => {
    const text = textOf(codeEl?.props?.children).replace(/\n$/, "");
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard unavailable (e.g. insecure context) — leave state unchanged.
    }
  };

  return (
    <div className="markdown-codeblock">
      <div className="flex items-center justify-between rounded-t-md border border-b-0 border-border bg-surface-2 px-3 py-1.5 text-xs text-fg-muted">
        <span className="font-medium">{lang ?? "code"}</span>
        <button
          type="button"
          onClick={copy}
          className="rounded px-1.5 py-0.5 transition-colors hover:bg-surface-0 hover:text-fg"
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre>{children}</pre>
    </div>
  );
};

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
    return <>{content || "Thinking…"}</>;
  }

  return (
    <div className={`markdown-body ${className ?? ""}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[[rehypeHighlight, { detect: true, ignoreMissing: true }]]}
        components={{ pre: CodeBlock }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
};

/** Renders user messages as plain text (no markdown). */
export const PlainMessage: FC<{ content: string }> = ({ content }) => <>{content}</>;
