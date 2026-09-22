import React, { Children, isValidElement, memo, useEffect, useState, type ReactNode } from "react";
import { Button } from "@heroui/react";
import { Check, Copy } from "@phosphor-icons/react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { safeAnswerLink } from "./interviewUiState";

function plainText(children: ReactNode): string {
  return Children.toArray(children).map((child): string => {
    if (typeof child === "string" || typeof child === "number") return String(child);
    return isValidElement<{ children?: ReactNode }>(child) ? plainText(child.props.children) : "";
  }).join("");
}

export function CopyTextButton({ text, label = "复制", className = "" }: { text: string; label?: string; className?: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  useEffect(() => {
    if (state === "idle") return;
    const timer = window.setTimeout(() => setState("idle"), 2_000);
    return () => window.clearTimeout(timer);
  }, [state]);
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setState("copied");
    } catch { setState("failed"); }
  }
  return <Button variant="ghost" size="sm" className={className} onPress={() => void copy()} isDisabled={!text} aria-label={label}>
    {state === "copied" ? <Check size={14} /> : <Copy size={14} />}
    <span role="status">{state === "copied" ? "已复制" : state === "failed" ? "复制失败" : label}</span>
  </Button>;
}

function CodeBlock({ children, onUseCode }: { children?: ReactNode; onUseCode?: (code: string, language: string) => void }) {
  const code = Children.toArray(children).find((child) => isValidElement(child));
  const language = isValidElement<{ className?: string }>(code)
    ? code.props.className?.match(/language-([\w+-]+)/)?.[1]
    : undefined;
  return <div className="answer-code">
    <div className="answer-code-header"><span>{language || "代码"}</span>
      {onUseCode && <Button size="sm" variant="ghost" onPress={() => onUseCode(plainText(children), language || "text")}>放入代码区</Button>}
      <CopyTextButton text={plainText(children)} label="复制代码" /></div>
    <pre tabIndex={0} aria-label={language ? `${language} 代码` : "代码"}>{children}</pre>
  </div>;
}

const markdownComponents: Components = {
  pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
  a: ({ children, href }) => safeAnswerLink(href)
    ? <a href={safeAnswerLink(href)} target="_blank" rel="noreferrer noopener">{children}</a>
    : <span>{children}</span>,
  table: ({ children }) => <div className="answer-table" role="region" aria-label="回答表格" tabIndex={0}><table>{children}</table></div>,
};

export const AnswerMarkdown = memo(function AnswerMarkdown({ text, onUseCode }: { text: string; onUseCode?: (code: string, language: string) => void }) {
  return <div className="answer-markdown">
    <Markdown
      remarkPlugins={[remarkGfm]}
      skipHtml
      disallowedElements={["img"]}
      urlTransform={(url) => safeAnswerLink(url)}
      components={onUseCode ? { ...markdownComponents, pre: ({ children }) => <CodeBlock onUseCode={onUseCode}>{children}</CodeBlock> } : markdownComponents}
    >{text}</Markdown>
  </div>;
});
