/** ヘルプポップアップの内容(feedback 20260620 #4)。
 *  各機能ページの「?」を開くと、アーキ図(構成図)とコア機能の短い説明を表示する。
 *  diagram は public/architecture/ に prebuild でコピーされる(正本は docs/architecture/usecases)。
 *  titleKey/descKey は i18n のキー(dict.ja.ts / dict.en.ts に help.* を定義)。 */
export type HelpKey =
  | 'chat' | 'rag' | 'dbchat' | 'agents' | 'minutes'
  | 'realtime' | 'voicechat' | 'video' | 'ocr'

export type HelpEntry = { diagram: string; titleKey: string; descKey: string }

export const HELP_CONTENT: Record<HelpKey, HelpEntry> = {
  chat:      { diagram: '/architecture/usecase-chat.png',      titleKey: 'help.chat.title',      descKey: 'help.chat.desc' },
  rag:       { diagram: '/architecture/usecase-rag.png',       titleKey: 'help.rag.title',       descKey: 'help.rag.desc' },
  dbchat:    { diagram: '/architecture/usecase-dbchat.png',    titleKey: 'help.dbchat.title',    descKey: 'help.dbchat.desc' },
  agents:    { diagram: '/architecture/usecase-agent.png',     titleKey: 'help.agents.title',    descKey: 'help.agents.desc' },
  minutes:   { diagram: '/architecture/usecase-minutes.png',   titleKey: 'help.minutes.title',   descKey: 'help.minutes.desc' },
  realtime:  { diagram: '/architecture/usecase-realtime.png',  titleKey: 'help.realtime.title',  descKey: 'help.realtime.desc' },
  voicechat: { diagram: '/architecture/usecase-voicechat.png', titleKey: 'help.voicechat.title', descKey: 'help.voicechat.desc' },
  video:     { diagram: '/architecture/usecase-video.png',     titleKey: 'help.video.title',     descKey: 'help.video.desc' },
  ocr:       { diagram: '/architecture/usecase-ocr.png',       titleKey: 'help.ocr.title',       descKey: 'help.ocr.desc' },
}
