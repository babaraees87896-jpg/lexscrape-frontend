// Run: node extract_key.js
const fs = require('fs');
const CryptoJS = require('crypto-js');

let code = fs.readFileSync(__dirname + '/obfusc_block.js', 'utf8');
code = code.replace('const _0x895125=', 'globalThis._0x895125=');
eval(code);

console.log('AES key (_0x895125):', globalThis._0x895125);
const key = globalThis._0x895125;

// decrypt sample
const sample = JSON.parse(fs.readFileSync(__dirname + '/../sample_response.json', 'utf8'));
const decrypted = globalThis._0x895125d(sample.data, CryptoJS);
console.log('Sample decrypt OK, keys:', Object.keys(decrypted).slice(0, 10));
console.log(JSON.stringify(decrypted, null, 2).slice(0, 1500));
