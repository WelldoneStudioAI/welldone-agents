/**
 * framer_helper.js — Pont Node.js entre le bot Python et l'API Framer (WebSocket SDK)
 *
 * Usage (appelé en subprocess depuis agents/framer.py) :
 *   node framer_helper.js schema            → JSON des champs de la collection
 *   node framer_helper.js list              → JSON des articles existants
 *   node framer_helper.js create '<json>'   → Crée un article, retourne {success, id}
 *   node framer_helper.js delete '<id>'     → Supprime un article
 *
 * Variables d'env requises :
 *   FRAMER_API_KEY        → token fr_xxx
 *   FRAMER_COLLECTION_ID  → ID de la collection CMS (ex: fNsEoxwRx)
 */

import { connect } from "framer-api"

const PROJECT_URL    = "https://framer.com/projects/Welldone-Studio--nghGT4Mav9pHCoHxYhyn-cuMch"
const API_KEY        = process.env.FRAMER_API_KEY
const COLLECTION_ID  = process.env.FRAMER_COLLECTION_ID || "ERDJzzQHr"

// ── Helpers ──────────────────────────────────────────────────────────────────

function ok(data)  { console.log(JSON.stringify({ ok: true,  ...data })); process.exit(0) }
function err(msg)  { console.log(JSON.stringify({ ok: false, error: String(msg) })); process.exit(1) }

async function getCollection(framer) {
  // 1. Chercher par ID dans toutes les collections
  try {
    const all = await framer.getCollections()
    const found = all.find(c => c.id === COLLECTION_ID)
    if (found) return found
  } catch (_) {}

  // 2. Fallback: collection gérée (plugin-managed)
  try {
    const managed = await framer.getManagedCollection()
    if (managed) return managed
  } catch (_) {}

  throw new Error(`Collection introuvable: ${COLLECTION_ID}`)
}

// ── Inférer le schéma depuis les items existants ──────────────────────────────
function inferSchema(items) {
  if (!items || items.length === 0) return []
  // Prendre le premier item avec des fieldData
  const sample = items.find(i => i.fieldData && Object.keys(i.fieldData).length > 0)
  if (!sample) return []
  return Object.entries(sample.fieldData).map(([id, field]) => ({
    id,
    type: field.type || "string",
    // On ne connaît pas le nom — sera résolu par Claude depuis le contexte
  }))
}

// ── Main ──────────────────────────────────────────────────────────────────────

async function main() {
  if (!API_KEY) return err("FRAMER_API_KEY manquant")

  const [,, command, ...rest] = process.argv
  const arg = rest.join(" ").trim()

  let framer, collection
  try {
    framer     = await connect(PROJECT_URL, API_KEY)
    collection = await getCollection(framer)
  } catch (e) {
    return err(`Connexion Framer échouée: ${e.message}`)
  }

  // ── schema ──────────────────────────────────────────────────────────────────
  if (command === "schema") {
    try {
      // Essayer d'abord via la propriété fields si disponible
      let fields = []
      if (collection.fields && Array.isArray(collection.fields)) {
        fields = collection.fields.map(f => ({ id: f.id, name: f.name || f.id, type: f.type || "string" }))
      } else {
        // Inférer depuis les items
        const items = await collection.getItems()
        fields = inferSchema(items)
        // Enrichir avec le premier item pour avoir les valeurs
        if (items.length > 0 && items[0].fieldData) {
          const sample = items[0]
          fields = fields.map(f => ({ ...f, example: sample.fieldData[f.id]?.value }))
        }
      }
      return ok({ collection_id: collection.id, fields })
    } catch (e) {
      return err(`schema error: ${e.message}`)
    }
  }

  // ── list ────────────────────────────────────────────────────────────────────
  if (command === "list") {
    try {
      const items = await collection.getItems()
      const simplified = items.map(item => {
        const fd = item.fieldData || {}
        // Chercher le titre (premier champ string non-vide)
        const titleField = Object.values(fd).find(f => f.type === "string" && f.value)
        return {
          id:        item.id,
          slug:      item.slug,
          title:     titleField?.value || item.slug || "(sans titre)",
          published: fd.published?.value ?? false,
          date:      fd.date?.value || null,
        }
      })
      return ok({ items: simplified, count: simplified.length })
    } catch (e) {
      return err(`list error: ${e.message}`)
    }
  }

  // ── create ──────────────────────────────────────────────────────────────────
  if (command === "create") {
    if (!arg) return err("create: JSON manquant en argument")
    let data
    try {
      data = JSON.parse(arg)
    } catch (e) {
      return err(`create: JSON invalide — ${e.message}`)
    }

    try {
      // data doit avoir: { slug, fieldData: { fieldId: { value, type } } }
      await collection.addItems([data])
      return ok({ message: "Article créé dans Framer CMS" })
    } catch (e) {
      return err(`create error: ${e.message}`)
    }
  }

  // ── update ──────────────────────────────────────────────────────────────────
  // Usage: node framer_helper.js update '<id>' '<json_fieldData>'
  // json_fieldData: { "fieldId": { "type": "string", "value": "..." }, ... }
  if (command === "update") {
    const [itemId, ...jsonParts] = rest
    const jsonStr = jsonParts.join(" ").trim()
    if (!itemId || !jsonStr) return err("update: ID et JSON requis")

    let fieldData
    try {
      fieldData = JSON.parse(jsonStr)
    } catch (e) {
      return err(`update: JSON invalide — ${e.message}`)
    }

    try {
      const items = await collection.getItems()
      const item = items.find(i => i.id === itemId)
      if (!item) return err(`update: item ${itemId} introuvable`)

      // Utiliser setAttributes avec seulement les champs à modifier
      // (évite de re-soumettre les champs image/file qui ont des types complexes)
      if (typeof item.setAttributes === "function") {
        await item.setAttributes({ fieldData })
      } else {
        // Fallback: patcher seulement les champs texte dans le fieldData existant
        const patched = {}
        for (const [fid, existingVal] of Object.entries(item.fieldData)) {
          if (fieldData[fid] !== undefined) {
            patched[fid] = fieldData[fid]
          } else if (existingVal.type === "string" || existingVal.type === "boolean" ||
                     existingVal.type === "number" || existingVal.type === "link" ||
                     existingVal.type === "enum") {
            patched[fid] = existingVal
          }
          // Skip image/file/array/formattedText qui causent des erreurs de validation
        }
        await collection.addItems([{
          id:        item.id,
          slug:      item.slug,
          draft:     item.draft ?? false,
          fieldData: patched,
        }])
      }
      return ok({ message: `Item ${itemId} mis à jour`, fields: Object.keys(fieldData) })
    } catch (e) {
      return err(`update error: ${e.message}`)
    }
  }

  // ── delete ──────────────────────────────────────────────────────────────────
  if (command === "delete") {
    if (!arg) return err("delete: ID manquant")
    try {
      await collection.removeItems([{ id: arg }])
      return ok({ message: `Article ${arg} supprimé` })
    } catch (e) {
      return err(`delete error: ${e.message}`)
    }
  }

  // ── batch-update ─────────────────────────────────────────────────────────────
  // Usage: node framer_helper.js batch-update '<json_array>'
  // json_array: [{ "id": "xxx", "slug": "new-slug", "fieldData": { ... } }, ...]
  // slug est optionnel. fieldData contient seulement les champs à modifier.
  // Une seule connexion WebSocket pour N mises à jour → 10-20x plus rapide.
  if (command === "batch-update") {
    if (!arg) return err("batch-update: JSON array manquant")
    let updates
    try {
      updates = JSON.parse(arg)
      if (!Array.isArray(updates)) throw new Error("Doit être un tableau JSON")
    } catch (e) {
      return err(`batch-update: JSON invalide — ${e.message}`)
    }

    const items = await collection.getItems()
    const results = []

    for (const u of updates) {
      const { id: itemId, slug: newSlug, fieldData } = u
      if (!itemId) { results.push({ id: itemId, ok: false, error: "id manquant" }); continue }

      const item = items.find(i => i.id === itemId)
      if (!item) { results.push({ id: itemId, ok: false, error: "introuvable" }); continue }

      try {
        const attrs = {}
        if (newSlug)    attrs.slug      = newSlug
        if (fieldData)  attrs.fieldData = fieldData
        await item.setAttributes(attrs)
        results.push({ id: itemId, ok: true })
      } catch (e) {
        results.push({ id: itemId, ok: false, error: e.message })
      }
    }

    const failed = results.filter(r => !r.ok)
    return ok({
      message: `${results.length - failed.length}/${results.length} mises à jour réussies`,
      results,
    })
  }

  // ── links ───────────────────────────────────────────────────────────────────
  // Usage: node framer_helper.js links
  // Retourne { slug: external_url } pour tous les items qui ont un champ link rempli.
  // Utilisé par l'endpoint Railway GET /archi-news-links pour le redirect dynamique.
  if (command === "links") {
    const LINK_FIELD = "MHz47j4CF"
    try {
      const items = await collection.getItems()
      const links = {}
      for (const item of items) {
        const fd = item.fieldData || {}
        const url = fd[LINK_FIELD]?.value
        if (item.slug && url && url !== "null") {
          links[item.slug] = url
        }
      }
      return ok({ links, count: Object.keys(links).length })
    } catch (e) {
      return err(`links error: ${e.message}`)
    }
  }

  return err(`Commande inconnue: ${command}. Disponibles: schema, list, create, update, batch-update, delete, links`)
}

main().catch(e => err(e.message))
