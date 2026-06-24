# Redwood トークン抽出レポート (UI-01)

- 抽出元: `@oracle/oraclejet` `dist/css/redwood/oj-redwood.css`
- 抽出数: ライト 1066 変数 / ダーク(再定義) 374 変数
- 出力: `packages/web/src/styles/tokens.css`
- ルール: ファイル内最初の定義=ライト、2回目以降=ダーク。`var()` 多段参照は実値へ再帰解決し、`rgb(R,G,B)` は hex に正規化

## Brand パレット (--oj-palette-brand-rgb-*)

| 変数 | ライト | ダーク |
|---|---|---|
| `--oj-palette-brand-rgb-10` | `246, 250, 252` | `254, 249, 233` |
| `--oj-palette-brand-rgb-20` | `237, 246, 249` | `253, 244, 223` |
| `--oj-palette-brand-rgb-30` | `228, 241, 247` | `252, 239, 203` |
| `--oj-palette-brand-rgb-40` | `208, 229, 238` | `247, 224, 161` |
| `--oj-palette-brand-rgb-50` | `180, 213, 225` | `240, 204, 113` |
| `--oj-palette-brand-rgb-60` | `143, 191, 208` | `213, 179, 100` |
| `--oj-palette-brand-rgb-70` | `121, 177, 198` | `199, 165, 93` |
| `--oj-palette-brand-rgb-80` | `95, 162, 186` | `179, 149, 84` |
| `--oj-palette-brand-rgb-90` | `65, 144, 172` | `157, 130, 73` |
| `--oj-palette-brand-rgb-100` | `34, 126, 158` | `137, 114, 63` |
| `--oj-palette-brand-rgb-110` | `14, 114, 151` | `113, 94, 52` |
| `--oj-palette-brand-rgb-120` | `0, 104, 140` | `113, 94, 52` |
| `--oj-palette-brand-rgb-130` | `2, 94, 126` | `102, 85, 47` |
| `--oj-palette-brand-rgb-140` | `4, 83, 111` | `91, 74, 41` |
| `--oj-palette-brand-rgb-150` | `6, 72, 95` | `79, 66, 36` |
| `--oj-palette-brand-rgb-160` | `6, 60, 78` | `64, 54, 29` |
| `--oj-palette-brand-rgb-170` | `5, 50, 66` | `55, 44, 24` |


## Neutral パレット (--oj-palette-neutral-rgb-*)

確認ポイント: **neutral-170 がヘッダー用ダーク色 (#312D2A 系) として抽出できているか**

| 変数 | ライト | ダーク |
|---|---|---|
| `--oj-palette-neutral-rgb-0` | `255, 255, 255` | `(ライトと同じ)` |
| `--oj-palette-neutral-rgb-10` | `251, 249, 248` | `(ライトと同じ)` |
| `--oj-palette-neutral-rgb-20` | `245, 244, 242` | `(ライトと同じ)` |
| `--oj-palette-neutral-rgb-30` | `241, 239, 237` | `(ライトと同じ)` |
| `--oj-palette-neutral-rgb-40` | `228, 225, 221` | `(ライトと同じ)` |
| `--oj-palette-neutral-rgb-50` | `212, 207, 202` | `(ライトと同じ)` |
| `--oj-palette-neutral-rgb-60` | `188, 182, 177` | `(ライトと同じ)` |
| `--oj-palette-neutral-rgb-70` | `174, 168, 162` | `(ライトと同じ)` |
| `--oj-palette-neutral-rgb-80` | `158, 152, 146` | `(ライトと同じ)` |
| `--oj-palette-neutral-rgb-90` | `139, 133, 128` | `(ライトと同じ)` |
| `--oj-palette-neutral-rgb-100` | `123, 117, 112` | `(ライトと同じ)` |
| `--oj-palette-neutral-rgb-110` | `111, 105, 100` | `(ライトと同じ)` |
| `--oj-palette-neutral-rgb-120` | `101, 95, 91` | `(ライトと同じ)` |
| `--oj-palette-neutral-rgb-130` | `92, 86, 81` | `(ライトと同じ)` |
| `--oj-palette-neutral-rgb-140` | `81, 76, 71` | `(ライトと同じ)` |
| `--oj-palette-neutral-rgb-150` | `71, 66, 62` | `(ライトと同じ)` |
| `--oj-palette-neutral-rgb-160` | `58, 54, 50` | `(ライトと同じ)` |
| `--oj-palette-neutral-rgb-170` | `49, 45, 42` | `(ライトと同じ)` |
| `--oj-palette-neutral-rgb-180` | `32, 30, 28` | `(ライトと同じ)` |
| `--oj-palette-neutral-rgb-190` | `22, 21, 19` | `(ライトと同じ)` |
| `--oj-palette-neutral-rgb-200` | `00, 00, 00` | `(ライトと同じ)` |


## Danger / Warning / Success / Info パレット

| 変数 | ライト | ダーク |
|---|---|---|
| `--oj-palette-danger-rgb-10` | `255, 248, 247` | `(ライトと同じ)` |
| `--oj-palette-danger-rgb-100` | `214, 59, 37` | `(ライトと同じ)` |
| `--oj-palette-danger-rgb-110` | `195, 53, 34` | `(ライトと同じ)` |
| `--oj-palette-danger-rgb-120` | `179, 49, 31` | `(ライトと同じ)` |
| `--oj-palette-danger-rgb-130` | `170, 34, 34` | `(ライトと同じ)` |
| `--oj-palette-danger-rgb-140` | `143, 39, 25` | `(ライトと同じ)` |
| `--oj-palette-danger-rgb-150` | `124, 34, 22` | `(ライトと同じ)` |
| `--oj-palette-danger-rgb-160` | `102, 28, 18` | `(ライトと同じ)` |
| `--oj-palette-danger-rgb-170` | `86, 24, 15` | `(ライトと同じ)` |
| `--oj-palette-danger-rgb-20` | `255, 241, 239` | `(ライトと同じ)` |
| `--oj-palette-danger-rgb-30` | `255, 235, 232` | `(ライトと同じ)` |
| `--oj-palette-danger-rgb-40` | `255, 217, 211` | `(ライトと同じ)` |
| `--oj-palette-danger-rgb-50` | `255, 193, 184` | `(ライトと同じ)` |
| `--oj-palette-danger-rgb-60` | `255, 157, 144` | `(ライトと同じ)` |
| `--oj-palette-danger-rgb-70` | `255, 134, 117` | `(ライトと同じ)` |
| `--oj-palette-danger-rgb-80` | `254, 104, 84` | `(ライトと同じ)` |
| `--oj-palette-danger-rgb-90` | `236, 79, 58` | `(ライトと同じ)` |
| `--oj-palette-info-rgb-10` | `246, 250, 252` | `(ライトと同じ)` |
| `--oj-palette-info-rgb-100` | `34, 126, 158` | `(ライトと同じ)` |
| `--oj-palette-info-rgb-110` | `14, 114, 151` | `(ライトと同じ)` |
| `--oj-palette-info-rgb-120` | `0, 104, 140` | `(ライトと同じ)` |
| `--oj-palette-info-rgb-130` | `2, 94, 126` | `(ライトと同じ)` |
| `--oj-palette-info-rgb-140` | `4, 83, 111` | `(ライトと同じ)` |
| `--oj-palette-info-rgb-150` | `6, 72, 95` | `(ライトと同じ)` |
| `--oj-palette-info-rgb-160` | `6, 60, 78` | `(ライトと同じ)` |
| `--oj-palette-info-rgb-170` | `5, 50, 66` | `(ライトと同じ)` |
| `--oj-palette-info-rgb-20` | `237, 246, 249` | `(ライトと同じ)` |
| `--oj-palette-info-rgb-30` | `228, 241, 247` | `(ライトと同じ)` |
| `--oj-palette-info-rgb-40` | `208, 229, 238` | `(ライトと同じ)` |
| `--oj-palette-info-rgb-50` | `180, 213, 225` | `(ライトと同じ)` |
| `--oj-palette-info-rgb-60` | `143, 191, 208` | `(ライトと同じ)` |
| `--oj-palette-info-rgb-70` | `121, 177, 198` | `(ライトと同じ)` |
| `--oj-palette-info-rgb-80` | `95, 162, 186` | `(ライトと同じ)` |
| `--oj-palette-info-rgb-90` | `65, 144, 172` | `(ライトと同じ)` |
| `--oj-palette-success-rgb-10` | `244, 252, 235` | `(ライトと同じ)` |
| `--oj-palette-success-rgb-100` | `80, 130, 35` | `(ライトと同じ)` |
| `--oj-palette-success-rgb-110` | `73, 118, 32` | `(ライトと同じ)` |
| `--oj-palette-success-rgb-120` | `67, 107, 29` | `(ライトと同じ)` |
| `--oj-palette-success-rgb-130` | `60, 96, 26` | `(ライトと同じ)` |
| `--oj-palette-success-rgb-140` | `53, 86, 23` | `(ライトと同じ)` |
| `--oj-palette-success-rgb-150` | `46, 73, 20` | `(ライトと同じ)` |
| `--oj-palette-success-rgb-160` | `38, 61, 16` | `(ライトと同じ)` |
| `--oj-palette-success-rgb-170` | `31, 51, 14` | `(ライトと同じ)` |
| `--oj-palette-success-rgb-20` | `235, 248, 222` | `(ライトと同じ)` |
| `--oj-palette-success-rgb-30` | `228, 245, 211` | `(ライトと同じ)` |
| `--oj-palette-success-rgb-40` | `207, 235, 179` | `(ライトと同じ)` |
| `--oj-palette-success-rgb-50` | `177, 221, 136` | `(ライトと同じ)` |
| `--oj-palette-success-rgb-60` | `138, 201, 79` | `(ライトと同じ)` |
| `--oj-palette-success-rgb-70` | `125, 186, 69` | `(ライトと同じ)` |
| `--oj-palette-success-rgb-80` | `111, 169, 57` | `(ライトと同じ)` |
| `--oj-palette-success-rgb-90` | `94, 148, 43` | `(ライトと同じ)` |
| `--oj-palette-warning-rgb-10` | `254, 249, 242` | `(ライトと同じ)` |
| `--oj-palette-warning-rgb-100` | `172, 99, 12` | `(ライトと同じ)` |
| `--oj-palette-warning-rgb-110` | `156, 89, 11` | `(ライトと同じ)` |
| `--oj-palette-warning-rgb-120` | `143, 82, 10` | `(ライトと同じ)` |
| `--oj-palette-warning-rgb-130` | `129, 73, 9` | `(ライトと同じ)` |
| `--oj-palette-warning-rgb-140` | `114, 65, 8` | `(ライトと同じ)` |
| `--oj-palette-warning-rgb-150` | `99, 56, 7` | `(ライトと同じ)` |
| `--oj-palette-warning-rgb-160` | `81, 47, 6` | `(ライトと同じ)` |
| `--oj-palette-warning-rgb-170` | `69, 39, 5` | `(ライトと同じ)` |
| `--oj-palette-warning-rgb-20` | `253, 242, 229` | `(ライトと同じ)` |
| `--oj-palette-warning-rgb-30` | `252, 237, 220` | `(ライトと同じ)` |
| `--oj-palette-warning-rgb-40` | `249, 221, 188` | `(ライトと同じ)` |
| `--oj-palette-warning-rgb-50` | `246, 199, 146` | `(ライトと同じ)` |
| `--oj-palette-warning-rgb-60` | `240, 169, 87` | `(ライトと同じ)` |
| `--oj-palette-warning-rgb-70` | `235, 150, 50` | `(ライトと同じ)` |
| `--oj-palette-warning-rgb-80` | `225, 128, 18` | `(ライトと同じ)` |
| `--oj-palette-warning-rgb-90` | `198, 113, 14` | `(ライトと同じ)` |


## テキスト色 (core text)

| 変数 | ライト | ダーク |
|---|---|---|
| `--oj-core-text-color-brand` | `#0e7297` | `(ライトと同じ)` |
| `--oj-core-text-color-danger` | `#b3311f` | `#ff8675` |
| `--oj-core-text-color-disabled` | `rgba(22, 21, 19, .4)` | `rgba(255, 255, 255, 0.3)` |
| `--oj-core-text-color-primary` | `#161513` | `#ffffff` |
| `--oj-core-text-color-secondary` | `rgba(22, 21, 19, .70)` | `rgba(255, 255, 255, 0.7)` |
| `--oj-core-text-color-success` | `#436b1d` | `#7dba45` |
| `--oj-core-text-color-warning` | `#8f520a` | `#eb9632` |


## 背景・サーフェス色 (core bg / neutral色適用先)

| 変数 | ライト | ダーク |
|---|---|---|
| `--oj-core-bg-color-active` | `rgba(22, 21, 19, .16)` | `rgba(255, 255, 255, 0.12)` |
| `--oj-core-bg-color-content` | `#ffffff` | `#312d2a` |
| `--oj-core-bg-color-hover` | `rgba(22, 21, 19, .08)` | `rgba(255, 255, 255, 0.08)` |
| `--oj-core-bg-color-selected` | `#e4f1f7` | `(ライトと同じ)` |
| `--oj-core-neutral-1` | `#7b7570` | `#9e9892` |
| `--oj-core-neutral-2` | `#6f6964` | `#8b8580` |
| `--oj-core-neutral-3` | `#655f5b` | `#aea8a2` |
| `--oj-core-neutral-contrast` | `#ffffff` | `#161513` |
| `--oj-core-neutral-secondary-1` | `rgba(22, 21, 19, 0.08)` | `rgba(255, 255, 255, 0.16)` |
| `--oj-core-neutral-secondary-2` | `#f5f4f2` | `#47423e` |
| `--oj-core-neutral-secondary-3` | `#fbf9f8` | `#3a3632` |
| `--oj-core-neutral-secondary-contrast` | `#161513` | `#ffffff` |


## ボーダー・分割線

| 変数 | ライト | ダーク |
|---|---|---|
| `--oj-button-borderless-chrome-border-color-active` | `transparent` | `transparent` |
| `--oj-button-borderless-chrome-border-color-hover` | `transparent` | `transparent` |
| `--oj-button-borderless-chrome-border-color-selected` | `#227e9e` | `#f0cc71` |
| `--oj-button-borderless-chrome-border-color-selected-disabled` | `rgba(22, 21, 19,.20)` | `rgba(255, 255, 255, .6)` |
| `--oj-button-borderless-chrome-border-color-selected-hover` | `#227e9e` | `#89723f` |
| `--oj-button-call-to-action-chrome-border-color` | `transparent` | `transparent` |
| `--oj-button-call-to-action-chrome-border-color-active` | `transparent` | `transparent` |
| `--oj-button-call-to-action-chrome-border-color-hover` | `transparent` | `transparent` |
| `--oj-button-outlined-chrome-border-color` | `rgba(22, 21, 19, 0.5)` | `rgba(255, 255, 255, 0.5)` |
| `--oj-button-outlined-chrome-border-color-active` | `rgba(22, 21, 19, 0.5)` | `rgba(255, 255, 255, 0.5)` |
| `--oj-button-outlined-chrome-border-color-disabled` | `rgba(22, 21, 19, .4)` | `rgba(255, 255, 255, .1)` |
| `--oj-button-outlined-chrome-border-color-hover` | `rgba(22, 21, 19, 0.5)` | `rgba(255, 255, 255, 0.5)` |
| `--oj-button-outlined-chrome-border-color-selected` | `#227e9e` | `#f0cc71` |
| `--oj-button-outlined-chrome-border-color-selected-disabled` | `rgba(22, 21, 19, .4)` | `rgba(255, 255, 255, .1)` |
| `--oj-button-outlined-chrome-border-color-selected-hover` | `#227e9e` | `#89723f` |
| `--oj-button-solid-chrome-border-color` | `transparent` | `transparent` |
| `--oj-button-solid-chrome-border-color-active` | `transparent` | `transparent` |
| `--oj-button-solid-chrome-border-color-disabled` | `transparent` | `transparent` |
| `--oj-button-solid-chrome-border-color-hover` | `transparent` | `transparent` |
| `--oj-button-solid-chrome-border-color-selected` | `#227e9e` | `transparent` |
| `--oj-button-solid-chrome-border-color-selected-disabled` | `rgba(22, 21, 19, .4)` | `transparent` |
| `--oj-button-solid-chrome-border-color-selected-hover` | `#227e9e` | `transparent` |
| `--oj-buttonset-borderless-chrome-internal-border-color-selected` | `#b4d5e1` | `(ライトと同じ)` |
| `--oj-buttonset-outlined-chrome-internal-border-color` | `rgba(22, 21, 19, 0.5)` | `rgba(255, 255, 255, 0.5)` |
| `--oj-buttonset-outlined-chrome-internal-border-color-active` | `rgba(22, 21, 19, 0.5)` | `(ライトと同じ)` |
| `--oj-buttonset-outlined-chrome-internal-border-color-selected` | `rgba(22, 21, 19, 0.5)` | `#f0cc71` |
| `--oj-buttonset-outlined-chrome-internal-border-color-selected-disabled` | `rgba(22, 21, 19, 0.04)` | `(ライトと同じ)` |
| `--oj-collection-border-color` | `rgba(22, 21, 19, .1)` | `(ライトと同じ)` |
| `--oj-collection-editable-cell-border-color-focus` | `#227e9e` | `(ライトと同じ)` |
| `--oj-color-palette-swatch-inner-border-color` | `#312d2a` | `(ライトと同じ)` |


## Border radius

| 変数 | ライト | ダーク |
|---|---|---|
| `--oj-avatar-border-radius` | `.5rem` | `6px` |
| `--oj-badge-border-radius` | `0.75rem` | `6px` |
| `--oj-button-border-radius` | `.25rem` | `(ライトと同じ)` |
| `--oj-color-palette-border-radius` | `50%` | `(ライトと同じ)` |
| `--oj-core-border-radius-lg` | `.375rem` | `(ライトと同じ)` |
| `--oj-core-border-radius-md` | `.25rem` | `(ライトと同じ)` |
| `--oj-core-border-radius-sm` | `2px` | `(ライトと同じ)` |
| `--oj-core-border-radius-xl` | `.5rem` | `(ライトと同じ)` |
| `--oj-dialog-border-radius` | `.375rem` | `6px` |
| `--oj-file-picker-border-radius` | `.5rem` | `(ライトと同じ)` |
| `--oj-panel-border-radius` | `.375rem` | `(ライトと同じ)` |
| `--oj-popup-border-radius` | `2px` | `(ライトと同じ)` |
| `--oj-private-message-component-inline-border-radius` | `0` | `(ライトと同じ)` |
| `--oj-private-message-general-overlay-border-radius` | `.375rem` | `(ライトと同じ)` |
| `--oj-private-message-notification-overlay-border-radius` | `.375rem` | `(ライトと同じ)` |
| `--oj-private-messages-general-overlay-border-radius` | `2px` | `(ライトと同じ)` |
| `--oj-private-messages-notification-overlay-border-radius` | `initial` | `(ライトと同じ)` |
| `--oj-private-tab-bar-border-radius-bottom-left` | `0` | `(ライトと同じ)` |
| `--oj-private-tab-bar-border-radius-bottom-right` | `0` | `(ライトと同じ)` |
| `--oj-private-tab-bar-border-radius-top-left` | `0` | `(ライトと同じ)` |
| `--oj-private-tab-bar-border-radius-top-right` | `0` | `(ライトと同じ)` |
| `--oj-private-timeline-item-border-radius` | `0.375rem` | `(ライトと同じ)` |
| `--oj-private-timeline-item-duration-event-overflow-border-radius` | `0.375rem` | `(ライトと同じ)` |
| `--oj-progress-bar-border-radius` | `3px` | `(ライトと同じ)` |
| `--oj-slider-thumb-border-radius` | `.375rem` | `(ライトと同じ)` |
| `--oj-switch-thumb-border-radius` | `.25rem` | `(ライトと同じ)` |
| `--oj-switch-track-border-radius` | `.375rem` | `(ライトと同じ)` |
| `--oj-text-field-border-radius` | `.25rem` | `(ライトと同じ)` |
| `--oj-tooltip-border-radius` | `.25rem` | `(ライトと同じ)` |
| `--oj-train-step-border-radius` | `.5rem` | `(ライトと同じ)` |


## Box shadow

| 変数 | ライト | ダーク |
|---|---|---|
| `--oj-conveyor-belt-box-shadow-width` | `0.25rem` | `(ライトと同じ)` |
| `--oj-core-box-shadow-lg` | `0px 8px 16px 0px rgba(00, 00, 00,0.24)` | `0px 8px 16px 0px rgba(00, 00, 00, 0.24)` |
| `--oj-core-box-shadow-md` | `0px 6px 12px 0px rgba(00, 00, 00,.2)` | `0px 6px 12px 0px rgba(00, 00, 00, 0.2)` |
| `--oj-core-box-shadow-rgb` | `00, 00, 00` | `00, 00, 00` |
| `--oj-core-box-shadow-sm` | `0px 4px 8px 0px rgba(00, 00, 00,.16)` | `0px 4px 8px 0px rgba(00, 00, 00, 0.16)` |
| `--oj-core-box-shadow-xl` | `0px 12px 20px 0px rgba(00, 00, 00,0.28)` | `0px 12px 20px 0px rgba(00, 00, 00, 0.28)` |
| `--oj-core-box-shadow-xs` | `0px 1px 4px 0px rgba(00, 00, 00,.12)` | `0px 1px 4px 0px rgba(00, 00, 00, 0.12)` |
| `--oj-core-dropdown-box-shadow` | `0px 4px 8px 0px rgba(00, 00, 00,.16)` | `(ライトと同じ)` |
| `--oj-dialog-box-shadow` | `0px 12px 20px 0px rgba(00, 00, 00,0.28)` | `(ライトと同じ)` |
| `--oj-popup-box-shadow` | `0px 1px 4px 0px rgba(00, 00, 00,.12)` | `(ライトと同じ)` |
| `--oj-private-app-layout-hybrid-header-box-shadow` | `none` | `(ライトと同じ)` |
| `--oj-private-app-layout-hybrid-nav-bar-box-shadow` | `none` | `(ライトと同じ)` |
| `--oj-private-app-layout-web-header-box-shadow` | `none` | `(ライトと同じ)` |
| `--oj-private-message-general-overlay-box-shadow` | `0px 6px 12px 0px rgba(00, 00, 00,.2)` | `(ライトと同じ)` |
| `--oj-private-message-notification-overlay-box-shadow` | `0px 6px 12px 0px rgba(00, 00, 00,.2)` | `0px 6px 12px 0px rgba(00, 00, 00, 0.2)` |
| `--oj-private-messages-general-overlay-box-shadow` | `none` | `(ライトと同じ)` |
| `--oj-private-messages-notification-overlay-box-shadow` | `initial` | `(ライトと同じ)` |
| `--oj-slider-thumb-box-shadow` | `none` | `(ライトと同じ)` |
| `--oj-slider-thumb-box-shadow-active` | `none` | `(ライトと同じ)` |
| `--oj-slider-thumb-box-shadow-hover` | `none` | `(ライトと同じ)` |
| `--oj-switch-thumb-box-shadow` | `0px 0.125rem 0.25rem 0px rgba(00, 00, 00,.1)` | `(ライトと同じ)` |
| `--oj-switch-thumb-box-shadow-active` | `none` | `(ライトと同じ)` |
| `--oj-switch-thumb-box-shadow-hover` | `none` | `(ライトと同じ)` |
| `--oj-switch-thumb-box-shadow-selected` | `0px 0.125rem 0.25rem 0px rgba(00, 00, 00,.1)` | `(ライトと同じ)` |
| `--oj-switch-thumb-box-shadow-selected-active` | `none` | `(ライトと同じ)` |
| `--oj-switch-thumb-box-shadow-selected-hover` | `none` | `(ライトと同じ)` |
| `--oj-text-field-box-shadow-focus` | `0 0 0 1px #0e7297 inset` | `(ライトと同じ)` |


## Typography

| 変数 | ライト | ダーク |
|---|---|---|
| `--oj-typography-body-2xs-font-size` | `0.625rem` | `10px` |
| `--oj-typography-body-2xs-line-height` | `1.2` | `(ライトと同じ)` |
| `--oj-typography-body-lg-font-size` | `1.125rem` | `18px` |
| `--oj-typography-body-lg-line-height` | `1.3333` | `(ライトと同じ)` |
| `--oj-typography-body-md-font-size` | `1rem` | `16px` |
| `--oj-typography-body-md-line-height` | `1.25` | `(ライトと同じ)` |
| `--oj-typography-body-sm-font-size` | `0.859rem` | `13.744px` |
| `--oj-typography-body-sm-line-height` | `1.2` | `(ライトと同じ)` |
| `--oj-typography-body-xl-font-size` | `1.25rem` | `20px` |
| `--oj-typography-body-xl-line-height` | `1.4` | `(ライトと同じ)` |
| `--oj-typography-body-xs-font-size` | `0.75rem` | `12px` |
| `--oj-typography-body-xs-line-height` | `1.3333` | `(ライトと同じ)` |
| `--oj-typography-heading-2xl-font-size` | `2.5rem` | `40px` |
| `--oj-typography-heading-2xl-font-weight` | `800` | `(ライトと同じ)` |
| `--oj-typography-heading-2xl-line-height` | `1.3` | `1.2222` |
| `--oj-typography-heading-lg-font-size` | `2rem` | `32px` |
| `--oj-typography-heading-lg-font-weight` | `800` | `(ライトと同じ)` |
| `--oj-typography-heading-lg-line-height` | `1.25` | `1.2857` |
| `--oj-typography-heading-md-font-size` | `1.75rem` | `28px` |
| `--oj-typography-heading-md-font-weight` | `800` | `(ライトと同じ)` |
| `--oj-typography-heading-md-line-height` | `1.2857` | `1.3333` |
| `--oj-typography-heading-sm-font-size` | `1.5rem` | `24px` |
| `--oj-typography-heading-sm-font-weight` | `800` | `(ライトと同じ)` |
| `--oj-typography-heading-sm-line-height` | `1.3333` | `1.4` |
| `--oj-typography-heading-xl-font-size` | `2.25rem` | `36px` |
| `--oj-typography-heading-xl-font-weight` | `800` | `(ライトと同じ)` |
| `--oj-typography-heading-xl-line-height` | `1.222` | `1.25` |
| `--oj-typography-heading-xs-font-size` | `1.25rem` | `20px` |
| `--oj-typography-heading-xs-font-weight` | `800` | `(ライトと同じ)` |
| `--oj-typography-heading-xs-line-height` | `1.4` | `1.3333` |
| `--oj-typography-subheading-2xl-font-size` | `2.25rem` | `36px` |
| `--oj-typography-subheading-2xl-font-weight` | `bold` | `(ライトと同じ)` |
| `--oj-typography-subheading-2xl-line-height` | `1.2222` | `1.25` |
| `--oj-typography-subheading-lg-font-size` | `1.75rem` | `28px` |
| `--oj-typography-subheading-lg-font-weight` | `bold` | `(ライトと同じ)` |
| `--oj-typography-subheading-lg-line-height` | `1.2857` | `1.3333` |
| `--oj-typography-subheading-md-font-size` | `1.5rem` | `24px` |
| `--oj-typography-subheading-md-font-weight` | `bold` | `(ライトと同じ)` |
| `--oj-typography-subheading-md-line-height` | `1.3333` | `1.4` |
| `--oj-typography-subheading-sm-font-size` | `1.25rem` | `20px` |
| `--oj-typography-subheading-sm-font-weight` | `bold` | `(ライトと同じ)` |
| `--oj-typography-subheading-sm-line-height` | `1.4` | `1.3333` |
| `--oj-typography-subheading-xl-font-size` | `2rem` | `32px` |
| `--oj-typography-subheading-xl-font-weight` | `bold` | `(ライトと同じ)` |
| `--oj-typography-subheading-xl-line-height` | `1.25` | `1.2857` |
| `--oj-typography-subheading-xs-font-size` | `1rem` | `16px` |
| `--oj-typography-subheading-xs-font-weight` | `bold` | `(ライトと同じ)` |
| `--oj-typography-subheading-xs-line-height` | `1.5` | `1.4545` |


## リンク・フォーカス等のセマンティック色 (抜粋)

| 変数 | ライト | ダーク |
|---|---|---|
| `--oj-core-brand-1` | `#227e9e` | `#f0cc71` |
| `--oj-core-brand-2` | `#0e7297` | `#d5b364` |
| `--oj-core-brand-3` | `#00688c` | `#c7a55d` |
| `--oj-core-brand-contrast` | `#ffffff` | `#161513` |
| `--oj-core-drag-drop-color-1` | `#d0e5ee` | `(ライトと同じ)` |
| `--oj-core-drag-drop-color-2` | `#227e9e` | `(ライトと同じ)` |
| `--oj-core-drag-drop-line-color` | `#227e9e` | `(ライトと同じ)` |
| `--oj-core-focus-border-color` | `#161513` | `#ffffff` |

