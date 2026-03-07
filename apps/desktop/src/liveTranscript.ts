import { Speaker } from "./types";

const SENTENCE_ENDING = /[.?!]/;

export interface ParsedLiveChunk {
  speaker: Speaker;
  text: string;
}

export interface ExtractedTurns {
  completed: string[];
  remaining: string;
}

export function parseLiveScript(script: string): ParsedLiveChunk[] {
  return script
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const [rawSpeaker, ...rest] = line.split("|");
      const speaker = normalizeSpeaker(rawSpeaker.trim());
      return {
        speaker,
        text: rest.join("|").trim(),
      };
    })
    .filter((item) => item.text.length > 0);
}

export function joinChunk(buffer: string, chunk: string): string {
  const normalizedBuffer = buffer.trim();
  const normalizedChunk = chunk.trim();
  if (!normalizedBuffer) {
    return normalizedChunk;
  }
  if (!normalizedChunk) {
    return normalizedBuffer;
  }

  const lastChar = normalizedBuffer[normalizedBuffer.length - 1];
  const firstChar = normalizedChunk[0];
  const needsSpace = /[A-Za-z0-9]/.test(lastChar) && /[A-Za-z0-9]/.test(firstChar);
  return `${normalizedBuffer}${needsSpace ? " " : ""}${normalizedChunk}`;
}

export function extractCompletedTurns(buffer: string): ExtractedTurns {
  const completed: string[] = [];
  let start = 0;

  for (let index = 0; index < buffer.length; index += 1) {
    if (!SENTENCE_ENDING.test(buffer[index])) {
      continue;
    }
    const segment = buffer.slice(start, index + 1).trim();
    if (segment) {
      completed.push(segment);
    }
    start = index + 1;
  }

  return {
    completed,
    remaining: buffer.slice(start).trim(),
  };
}

function normalizeSpeaker(rawSpeaker: string): Speaker {
  const normalized = rawSpeaker.toLowerCase();
  if (normalized === "i" || normalized === "interviewer") {
    return "interviewer";
  }
  return "candidate";
}
