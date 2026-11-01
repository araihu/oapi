import { createPrivateKey } from 'node:crypto';
import { writeFileSync } from 'node:fs';

const appID = process.env.ARAIHU_ASSETS_APP_ID;
const sourceKey = process.env.ARAIHU_ASSETS_APP_PRIVATE_KEY;
if (!appID || !sourceKey || !process.argv[2]) throw new Error('Missing publisher App credentials or output path');
const key = createPrivateKey(sourceKey);
if (key.asymmetricKeyType !== 'rsa') throw new Error('GitHub App requires an RSA key');
// Web Crypto imports PKCS#8; GitHub-generated keys may arrive as PKCS#1.
writeFileSync(process.argv[2], JSON.stringify({
  GITHUB_APP_ID: appID,
  GITHUB_APP_PRIVATE_KEY: key.export({ type: 'pkcs8', format: 'pem' }),
}), { mode: 0o600, flag: 'wx' });
