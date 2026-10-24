import manrope from './fonts/Manrope.woff2';
import newsreader from './fonts/Newsreader.woff2';

// The existing server deliberately serves a small allowlist of assets. Bundling
// the font bytes with the public sign-in module keeps them self-hosted without
// adding a route, relaxing CSP, or making a request to a font service. ArrayBuffer
// FontFace sources are decoded locally rather than fetched as data/blob URLs.
async function installFonts() {
  document.documentElement.classList.add('fonts-loading');
  const faces = [
    new FontFace('Manrope', manrope, { style: 'normal', weight: '200 800', display: 'block' }),
    new FontFace('Newsreader', newsreader, { style: 'normal', weight: '200 800', display: 'block' }),
  ];
  for (const face of faces) document.fonts.add(face);
  try {
    await Promise.all(faces.map(face => face.load()));
    document.documentElement.dataset.fonts = 'ready';
  } finally {
    document.documentElement.classList.remove('fonts-loading');
  }
  return faces;
}

export const fontsReady = typeof document !== 'undefined' ? installFonts() : Promise.resolve([]);
