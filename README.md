# 素数大富豪＋

NEOと同じゲーム操作を使い、Classic・Plus・大会を表示する新クライアントです。

## ローカル表示

プロジェクト直下で静的サーバーを起動し、次を開きます。

```text
http://localhost:5174/qkplus-client-prototype/?ws=ws://localhost:8000/ws/plus
```

## 製品差

- Classic: `room_1`〜`room_6`
- Plus: `room_7`〜`room_9`
- 大会: 専用タブの大会ロビー `plus_tournament_1`。開催予定がある場合は別タブを選択中でも開始日時と大会名を表示
- 大会カテゴリには百鬼夜行の手動進行・練習用通常部屋 `hyakki_yagyo_1`〜`hyakki_yagyo_3` も表示。初期11枚、A〜Kはランクnごとにn枚＋X2枚の計93枚、32枚バースト。人間2人戦は1手60秒（サーバー設定の既定値）、CPU戦・1人練習は時間制限なし
- 大会入室後は画面タブを廃止。上部のタイトル・状態・メンバーと開閉できる大会情報を保ち、同じ位置に対戦・観戦一覧・個別観戦を表示。終了後の盤面を残し、観戦への移動はボタンで行う
- 大会チャットは「ロビー」「対戦」の2タブ。対戦履歴は同じページ・同じ大会の次戦へ引き継ぎ、終了後は閲覧専用（再読み込み時は保持しない）
- 観戦者は対戦ルームへ参加せず、場・手番・山札・残り手札枚数・接続状態だけを読み取り専用で購読
- 登録、アシスト、HNP、NEOキャンペーンは表示しない
- Classic / Plus / 百鬼夜行の通常部屋で使えるグローバルチャットに対応（大会ロビーでは大会専用チャットを表示）
- 青テーマ
- システム進行の定期大会に対応する。管理画面でラウンド固定／随時対戦を選択。随時対戦は途中参加期限あり・再戦なし・双方確認で開始・不在5分で棄権
- 順位は勝ち数で決定し同勝数は同順位。勝ち点表示なし。全ルール・部屋の待機中の切断猶予は1分
- Eventsは当面旧UIだけで表示する

ゲーム画面と通信処理は `../client-common/` を直接使用します。製品固有の変更は `product-config.js` と `theme.css` に置きます。

大会の管理画面:

```text
http://localhost:5174/qkplus-client-prototype/tournament-admin.html?ws=ws://localhost:8000/ws/plus
```

トップページからリンクしない合成数大富豪の専用練習ページ:

```text
http://localhost:5174/qkplus-client-prototype/composite-practice/?ws=ws://localhost:8000/ws/plus-practice
```

サーバー側に `COMPOSITE_PRACTICE_ACCESS_TOKEN` が必要です。専用部屋はDiscord入室通知、募集掲示板、グローバルチャットの対象外です。

サーバー側の設定と設計は `../TOURNAMENT_MODE.md` を参照してください。

別リポジトリ `greenplus/qkplus` へ置く自己完結版は次で生成します。

```powershell
.\scripts\build_web_clients.ps1 -Product plus
```

生成先は `outputs/web-clients/qkplus/` です。
