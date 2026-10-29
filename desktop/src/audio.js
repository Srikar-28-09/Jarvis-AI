export function encodePcmWav(chunks, sampleRate) {
  const totalSamples = chunks.reduce((total, chunk) => total + chunk.length, 0);
  if (!Number.isInteger(sampleRate) || sampleRate <= 0 || totalSamples === 0) {
    throw new Error('PCM audio requires samples and a valid sample rate');
  }

  const bytesPerSample = 2;
  const dataBytes = totalSamples * bytesPerSample;
  const buffer = new ArrayBuffer(44 + dataBytes);
  const view = new DataView(buffer);
  const writeTag = (offset, tag) => [...tag].forEach((character, index) => view.setUint8(offset + index, character.charCodeAt(0)));

  writeTag(0, 'RIFF');
  view.setUint32(4, 36 + dataBytes, true);
  writeTag(8, 'WAVE');
  writeTag(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * bytesPerSample, true);
  view.setUint16(32, bytesPerSample, true);
  view.setUint16(34, bytesPerSample * 8, true);
  writeTag(36, 'data');
  view.setUint32(40, dataBytes, true);

  let offset = 44;
  for (const chunk of chunks) {
    for (const value of chunk) {
      const sample = Math.max(-1, Math.min(1, value));
      view.setInt16(offset, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
      offset += bytesPerSample;
    }
  }

  return new Blob([buffer], { type: 'audio/wav' });
}
