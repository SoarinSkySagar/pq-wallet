# Leafguard (mobile app)

A React Native (Expo SDK 58) app for the Leafguard personal wallets (`pqv/wallet.py`). The phone is a screen: the
wallet's three JARDIN post-quantum keys, the signing and the EIP-8141 frame transactions stay on the wallet computer.
The app talks to that computer's wallet server over HTTPS (an ngrok tunnel) and sends the wallet's pairing code as a
Bearer token on every call. The pairing code is kept in the phone's keystore (`expo-secure-store`).

## Run it for a demo

On the wallet computer:

```sh
./demo.sh                  # local devnet, [BUDGET-010] mempool, wallets alice/bob/carol, explorer
./demo.sh tunnel alice     # ngrok tunnel to alice's wallet; prints the URL, the pairing code and a QR code
./demo.sh app              # Expo dev server (npm ci on first run)
```

On the phone: install **Expo Go** (Play Store / App Store) and open the `exp://` link that `./demo.sh app` prints
(scan its QR code: Android in Expo Go, iPhone with the Camera app; or *Enter URL manually* in Expo Go). The dev
server runs on port 8085 (8081 belongs to the Kurtosis engine) with `--offline`, so no Expo account or login is needed.
How the phone loads the app:

- same Wi-Fi as the computer: `./demo.sh app`
- any network, through your ngrok: `./demo.sh tunnel alice --app` then `./demo.sh app --ngrok`
  (one ngrok agent serves the wallet and the dev server; Expo Go loads `exp://<host>.ngrok-free.app:80`)
- Android over USB: `./demo.sh app --usb` (adb reverse, opens Expo Go on the phone by itself)

Expo's own `--tunnel` needs an Expo login and its ngrok package; the options above avoid both.

Then in Leafguard choose **Scan QR code** and scan the pairing QR from `./demo.sh tunnel` (also shown on the wallet's
**Phone** screen at http://127.0.0.1:8201 while the tunnel runs).

Free ngrok URLs change on every restart. Either use your account's free static domain
(`./demo.sh tunnel alice --url you.ngrok-free.app`) or update the URL in the app (Settings → Wallet URL, or
*Change the wallet URL* on the "cannot reach" screen; the pairing code is kept).

## Screens

Connect (scan or type URL + code) · Wallet (balance, activity, send / receive / add test ETH) · Send (contacts, scan
an address QR, Max) · Review (fee, gas and why it is higher: renewal, stateless signature, first use of a key) ·
progress of a payment step by step · Receive (QR, copy, share) · Keys (signatures left, renewals) · Vaults (invites,
create, accept, deploy, propose, approve, send) · Settings (wallet URL, node RPC and alt-mempool URLs used by the
wallet computer, disconnect).

## Develop

```sh
cd mobile
npm ci
npx expo start --offline --port 8085   # scan the QR with Expo Go; w = web
npx tsc --noEmit          # typecheck
npx expo-doctor
```

The web build (`npx expo export --platform web`) is for checking the screens in a browser. A browser enforces CORS
and the wallet server sends no CORS headers (on purpose: otherwise any web page could call a local wallet), so the
web build only talks to a wallet from a browser started with web security off. React Native has no CORS.

Layout: `App.tsx` (connection, navigation stack, polling, top and tab bars), `src/api.ts` (server client, pairing
storage), `src/format.ts` (exact BigInt amounts, avatars, fee reasons), `src/ui.tsx` (palette and components shared
with `app/wallet/index.html`), `src/screens/`.
