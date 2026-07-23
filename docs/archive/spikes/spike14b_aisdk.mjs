// SPIKE-14b (FW-03): Vercel AI SDK + OCI(IAM署名 custom fetch)
import { createOpenAI } from '@ai-sdk/openai';
import { generateText, streamText, tool } from 'ai';
import { z } from 'zod';
import common from 'oci-common';

const provider = new common.ConfigFileAuthenticationDetailsProvider();
const signer = new common.DefaultRequestSigner(provider);
const BASE = 'https://inference.generativeai.ap-osaka-1.oci.oraclecloud.com/openai/v1';

// IAM署名するcustom fetch
async function ociFetch(input, init = {}) {
  const url = typeof input === 'string' ? input : input.url;
  const headers = new Headers(init.headers || {});
  headers.set('CompartmentId', process.env.COMPARTMENT_OCID ?? (() => { throw new Error('COMPARTMENT_OCID required'); })());
  headers.set('OpenAi-Project', 'ocid1.generativeaiproject.oc1.ap-osaka-1.amaaaaaal7l2mtaafbc3hyhlw54smiwwnfmqpg2gdlmu6f363vj5q7zpbmba');
  headers.delete('authorization');
  const httpRequest = {
    uri: url,
    method: init.method || 'POST',
    headers,
    body: init.body,
  };
  await signer.signHttpRequest(httpRequest);
  return fetch(url, { ...init, headers: httpRequest.headers, duplex: 'half' });
}

const oci = createOpenAI({ baseURL: BASE, apiKey: 'OCI', fetch: ociFetch });
const model = oci.chat('openai.gpt-oss-120b');  // chat completions経路

// ① 基本
try {
  const { text } = await generateText({ model, prompt: '1+1は？数字のみ' });
  console.log('[OK] ①generateText:', text.slice(0, 30));
} catch (e) {
  console.log('[NG] ①generateText:', String(e).slice(0, 150));
}

// ② streaming
try {
  const { textStream } = streamText({ model, prompt: '1から3まで数えて' });
  let n = 0, acc = '';
  for await (const d of textStream) { n++; acc += d; }
  console.log('[OK] ②streamText:', n + 'チャンク /', acc.slice(0, 30));
} catch (e) {
  console.log('[NG] ②streamText:', String(e).slice(0, 150));
}

// ③ tool calling
try {
  const { text, steps } = await generateText({
    model,
    tools: {
      getWeather: tool({
        description: '都市の天気を返す',
        inputSchema: z.object({ city: z.string() }),
        execute: async ({ city }) => city + 'は晴れ、22度',
      }),
    },
    stopWhen: ({ steps }) => steps.length >= 4,
    prompt: '大阪の天気は？ツールで調べて日本語で',
  });
  console.log('[OK] ③tool:', (steps?.length ?? '?') + 'steps /', text.slice(0, 60));
} catch (e) {
  console.log('[NG] ③tool:', String(e).slice(0, 200));
}
