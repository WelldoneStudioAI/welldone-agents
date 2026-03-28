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

  return err(`Commande inconnue: ${command}. Disponibles: schema, list, create, delete`)
}

main().catch(e => err(e.message))
