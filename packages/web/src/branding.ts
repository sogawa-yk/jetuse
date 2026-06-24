/** branding.json を実行時に読み込み、CSS変数へ反映する（リブランド要件のデモ） */
export type Branding = {
  productName: string
  shortName: string
  logoText: string
  colors: { primary: string; primaryStrong: string; primarySoft: string }
}

export async function loadBranding(path = '/branding.json'): Promise<Branding> {
  const res = await fetch(path)
  const b: Branding = await res.json()
  applyBranding(b)
  return b
}

export function applyBranding(b: Branding) {
  const r = document.documentElement.style
  r.setProperty('--brand-primary', b.colors.primary)
  r.setProperty('--brand-primary-strong', b.colors.primaryStrong)
  r.setProperty('--brand-primary-soft', b.colors.primarySoft)
  document.title = b.productName
}
