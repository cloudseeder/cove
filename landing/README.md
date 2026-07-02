# landing — the love.cove.oap.dev marketing page

A single `index.html`. No build step, no dependencies, no framework. Edit and push.

## Structure

- `index.html` — the whole page, with inline `<style>`. Sections in scroll order: hero → three feature blocks → who it's for → how it works → trust callout → mission → footer.
- Palette: warm cream (`--bg`) and dark brown-black (`--fg`) with a gold accent (`--gold`). Automatic dark-mode variant flips to warm dark. Deliberately warmer than the app's utility-dark theme; this page should feel like reading a well-written article.
- Type: system serif (Iowan Old Style → Palatino → Georgia) for H1 / H2, system sans for everything else.
- Mobile-first responsive. One breakpoint at 480px.

## Deploy — Cloudflare Pages

Mirrors the setup for `app.cove.oap.dev`:

1. **Create the Pages project.**
   Cloudflare dashboard → Workers & Pages → Create → Pages → Connect to Git → pick `cloudseeder/cove`.
   Configure build:
     - Framework preset: **None**
     - Build command: *(leave empty)*
     - Build output directory: `landing`
     - Root directory: *(leave empty)*
   Save & deploy. First deploy takes ~30s.

2. **Add the custom domain.**
   In the Pages project → Custom domains → Set up a custom domain → enter `love.cove.oap.dev`. Because `oap.dev` is already at Cloudflare, the CNAME is created automatically. TLS provisions within a minute or two.

3. **Verify.**
   `https://love.cove.oap.dev` should serve `index.html`. If you see a 404, the build output directory is probably not set to `landing`.

## Iterating

Edit `index.html`, commit, push. Cloudflare Pages auto-rebuilds on every push to `main`. There's no CI step to wait for — plain-HTML deploys are effectively instant.

For a preview before merging, Cloudflare gives every non-`main` branch a preview URL (`<branch>.cove.pages.dev` or similar). Useful if we ever want to A/B a hero or workshop personas without touching the live site.
