window.PRIMEQK_CLIENT_CONFIG = {
  "productKey": "plus-private",
  "productName": "素数大富豪＋ プライベートルーム",
  "lobbyName": "プライベートロビー",
  "wsUrl": "wss://web-production-c8e68.up.railway.app/ws/plus-private",
  "shareUrl": "",
  "playerJoinedSoundUrl": "../assets/sounds/player-joined.mp3",
  "soundAssetsBaseUrl": "../assets/sounds",
  "maxRoomPlayers": 10,
  "defaultRoomGroupKey": "private",
  "defaultRoomKey": "private1",
  "roomGroupOrder": [
    "private"
  ],
  "roomGroups": {
    "private": {
      "label": "Private",
      "roomKeys": [
        "private1",
        "private2",
        "private3"
      ]
    }
  },
  "rooms": {
    "private1": {
      "roomId": "private_1",
      "roomGroupKey": "private",
      "roomNumber": 1,
      "label": "プライベート",
      "title": "11枚 / 通常",
      "summary": "合成数あり",
      "badge": "プライベート1 / 11枚 / 通常 / 合成数あり"
    },
    "private2": {
      "roomId": "private_2",
      "roomGroupKey": "private",
      "roomNumber": 2,
      "label": "プライベート",
      "title": "11枚 / 通常",
      "summary": "合成数あり",
      "badge": "プライベート2 / 11枚 / 通常 / 合成数あり"
    },
    "private3": {
      "roomId": "private_3",
      "roomGroupKey": "private",
      "roomNumber": 3,
      "label": "プライベート",
      "title": "11枚 / 通常",
      "summary": "合成数あり",
      "badge": "プライベート3 / 11枚 / 通常 / 合成数あり"
    }
  },
  "defaultSampleKey": "",
  "authMessageType": "authorize_private_rooms",
  "features": {
    "assist": false,
    "registration": false,
    "hnpChallenge": false,
    "campaign": false,
    "globalChat": false,
    "tournament": false,
    "recruitment": false,
    "turnAlternation": true,
    "practiceAuth": true
  }
};
