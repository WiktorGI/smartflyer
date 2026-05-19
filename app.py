import streamlit as st
import geopandas as gpd
import pandas as pd
import networkx as nx
from scipy.spatial import cKDTree
from pyproj import Geod
import folium
from streamlit_folium import st_folium
import datetime
from shapely.geometry import LineString, Point

# =========================================================
# 1. KONFIGURACJA STRONY I PAMIĘCI
# =========================================================
st.set_page_config(page_title="Multimodalny Podróżnik", layout="wide")
st.title("🌍 Multimodalna Wyszukiwarka Lotów")
st.markdown("Aplikacja oblicza najszybszą trasę łączącą dojazd autem do lotniska i lot docelowy, z wizualizacją na mapie.")

if 'wyszukano' not in st.session_state:
    st.session_state.wyszukano = False

# =========================================================
# 2. ŁADOWANIE DANYCH
# =========================================================
@st.cache_resource
def load_data():
    GPKG_FILE = "loty_polska_wynik.gpkg"
    CLEAN_ROADS = "drogi_czyste.parquet"
    CITIES_PARQUET = "ot_adms_p.parquet"

    lotniska = gpd.read_file(GPKG_FILE, layer="lotniska_punkty").to_crs("EPSG:2180")
    trasy = gpd.read_file(GPKG_FILE, layer="trasy_lotow")
    drogi = gpd.read_parquet(CLEAN_ROADS)
    miasta = gpd.read_parquet(CITIES_PARQUET)
    
    if miasta.crs is None or miasta.crs.to_string() != "EPSG:2180":
        miasta = miasta.to_crs("EPSG:2180")
        
    kolumna_rodzaj = next((col for col in miasta.columns if col.lower() == 'rodzaj'), None)
    if kolumna_rodzaj:
        miasta = miasta[miasta[kolumna_rodzaj].astype(str).str.lower() == 'miasto']

    cel_mapa = trasy.drop_duplicates('Destination_Code').set_index('Destination_Code')['Destination_City'].to_dict()

    speed_map = {
        'autostrada': 120, 'droga ekspresowa': 100, 
        'droga główna ruchu przyśpieszonego': 70, 
        'droga główna ruchu przyspieszonego': 70, 'droga główna': 50
    }
    G = nx.Graph()
    for _, row in drogi.iterrows():
        klasa = row.get('klasaDrogi', row.get('klasa_drog', ''))
        speed = speed_map.get(str(klasa).lower(), 50)
        coords = list(row.geometry.coords)
        G.add_edge(coords[0], coords[-1], weight=(row.geometry.length / 1000.0 / speed) * 60.0)

    nodes = list(G.nodes)
    tree = cKDTree(nodes)
    
    lotniska_węzły = {}
    for _, row in lotniska.iterrows():
        pt = row.geometry
        _, idx = tree.query((pt.x, pt.y))
        G.add_edge(f"LOTNISKO_{row['iata_code']}", nodes[idx], weight=90.0)
        lotniska_węzły[row['iata_code']] = f"LOTNISKO_{row['iata_code']}"

    geod = Geod(ellps="WGS84")
    def calc_flight_time(geom):
        dist_km = geod.geometry_length(geom) / 1000.0
        if dist_km < 500: v = 600
        elif dist_km < 3000: v = 800
        else: v = 880
        return 30 + (dist_km / v) * 60.0

    trasy_wgs = trasy.to_crs("EPSG:4326")
    trasy['flight_time_min'] = trasy_wgs.geometry.apply(calc_flight_time)

    dni_map = {"Poniedziałek": 0, "Wtorek": 1, "Środa": 2, "Czwartek": 3, "Piątek": 4, "Sobota": 5, "Niedziela": 6}
    trasy['dzien_num'] = trasy['Dzień'].map(dni_map)
    
    return lotniska, trasy, miasta, G, tree, nodes, lotniska_węzły, dni_map, cel_mapa

with st.spinner("Ładowanie bazy danych i topologii sieci dróg..."):
    lotniska, trasy, miasta, G, tree, nodes, lotniska_węzły, dni_map, cel_mapa = load_data()


# =========================================================
# 3. INTERFEJS UŻYTKOWNIKA
# =========================================================
with st.sidebar:
    st.header("Parametry podróży")
    
    lista_miast = sorted(miasta.get('nazwa', miasta.get('NAZWA')).dropna().unique())
    start_miasto = st.selectbox("Skąd wyruszasz?", options=lista_miast, index=lista_miast.index('Warszawa') if 'Warszawa' in lista_miast else 0)
    
    lista_celow_kody = sorted(cel_mapa.keys())
    
    def formatuj_cel(iata_code):
        miasto_docelowe = cel_mapa.get(iata_code, "Nieznane")
        return f"{miasto_docelowe} ({iata_code})"
    
    cel_iata = st.selectbox(
        "Dokąd lecisz?", 
        options=lista_celow_kody, 
        format_func=formatuj_cel,
        index=lista_celow_kody.index('ALC') if 'ALC' in lista_celow_kody else 0
    )
    
    wyjazd_dzien = st.selectbox("Dzień wyjazdu", list(dni_map.keys()))
    wyjazd_godzina = st.time_input("Godzina", datetime.time(8, 0))
    
    szukaj_btn = st.button("Szukaj optymalnego połączenia", type="primary")

    if szukaj_btn:
        st.session_state.wyszukano = True


# =========================================================
# 4. LOGIKA WYSZUKIWANIA I MAPA
# =========================================================
if st.session_state.wyszukano:
    
    pelna_nazwa_celu = formatuj_cel(cel_iata)
    st.subheader(f"Wyniki analizy dla: {start_miasto} ➔ {pelna_nazwa_celu}")
    
    nazwa_kolumny = 'nazwa' if 'nazwa' in miasta.columns else 'NAZWA'
    znalezione = miasta[miasta[nazwa_kolumny].str.lower() == start_miasto.lower()]
    
    if znalezione.empty:
        st.error("Nie znaleziono wybranego miasta w bazie przestrzennej.")
    else:
        pt_miasto_2180 = znalezione.geometry.iloc[0]
        dist_m, idx = tree.query((pt_miasto_2180.x, pt_miasto_2180.y))
        start_node = nodes[idx]
        czas_dojazdu_do_sieci_min = (dist_m / 1000.0 / 30.0) * 60.0 
        
        start_total_min = dni_map[wyjazd_dzien] * 24 * 60 + wyjazd_godzina.hour * 60 + wyjazd_godzina.minute
        
        dostepne_loty = trasy[trasy['Destination_Code'] == cel_iata].copy()
        
        if dostepne_loty.empty:
            st.warning("Brak bezpośrednich lotów na tej trasie z polskich lotnisk w aktualnej bazie.")
        else:
            potrzebne_lotniska = dostepne_loty['Origin'].unique()
            czasy_dojazdu = {}
            trasy_dojazdu = {}
            
            for iata in potrzebne_lotniska:
                if iata in lotniska_węzły:
                    try:
                        path_nodes = nx.shortest_path(G, source=start_node, target=lotniska_węzły[iata], weight='weight')
                        czas_droga = nx.path_weight(G, path_nodes, weight='weight')
                        czasy_dojazdu[iata] = czas_droga + czas_dojazdu_do_sieci_min
                        
                        coord_nodes = [node for node in path_nodes if isinstance(node, tuple)]
                        if len(coord_nodes) >= 2:
                            trasy_dojazdu[iata] = LineString(coord_nodes)
                            
                    except nx.NetworkXNoPath:
                        continue
                        
            wyniki = []
            for _, lot in dostepne_loty.iterrows():
                if lot['Origin'] not in czasy_dojazdu: continue
                
                czas_doj = czasy_dojazdu[lot['Origin']]
                czas_na_lotnisku = start_total_min + czas_doj
                lot_total_min = lot['dzien_num'] * 24 * 60 + int(str(lot['Time'])[:2]) * 60 + int(str(lot['Time'])[3:5])
                
                if lot_total_min < czas_na_lotnisku: 
                    lot_total_min += 7 * 24 * 60 
                    
                czas_oczek = lot_total_min - czas_na_lotnisku
                calkowity = czas_doj + czas_oczek + lot['flight_time_min']
                
                wyniki.append({
                    'Z Lotniska': lot['Origin'],
                    'Do': pelna_nazwa_celu,
                    'Linia': lot['Airline'],
                    'Dojazd autem + odprawa (h)': round(czas_doj/60, 1),
                    'Wylot': f"{lot['Dzień']} {lot['Time'][:5]}",
                    'Oczekiwanie na lot (h)': round(czas_oczek/60, 1),
                    'Lot (h)': round(lot['flight_time_min']/60, 1),
                    'CAŁKOWITY CZAS (h)': round(calkowity/60, 1),
                    'sort': calkowity
                })
            
            if not wyniki:
                st.warning("Brak dogodnych połączeń po uwzględnieniu dojazdu (brak dostępnej drogi do lotniska).")
            else:
                df_wyniki = pd.DataFrame(wyniki).sort_values('sort').drop(columns=['sort']).head(5).reset_index(drop=True)
                st.dataframe(df_wyniki, width='stretch')
                
                # ==================== MAPA FOLIUM ====================
                st.markdown("### Mapa wyznaczonych tras")
                miasto_wgs = gpd.GeoSeries([pt_miasto_2180], crs="EPSG:2180").to_crs("EPSG:4326").iloc[0]
                
                # Mapa odpala się scentrowana na Twoim mieście z polskim zoomem
                m = folium.Map(location=[miasto_wgs.y, miasto_wgs.x], zoom_start=6)
                
                # 1. MARKER: Dom (Start)
                folium.Marker(
                    location=[miasto_wgs.y, miasto_wgs.x], 
                    popup=f"Start: {start_miasto}", 
                    icon=folium.Icon(color="green", icon="home")
                ).add_to(m)
                
                uzyte_lotniska = df_wyniki['Z Lotniska'].unique()
                lotniska_wgs = lotniska.to_crs("EPSG:4326")
                
                for iata in uzyte_lotniska:
                    lot_dane = lotniska_wgs[lotniska_wgs['iata_code'] == iata]
                    if not lot_dane.empty:
                        pt_lot = lot_dane.geometry.iloc[0]
                        
                        # 2. MARKER: Lotnisko wylotu
                        folium.Marker(
                            location=[pt_lot.y, pt_lot.x], 
                            popup=f"Lotnisko {iata}", 
                            icon=folium.Icon(color="blue", icon="plane")
                        ).add_to(m)
                        
                        # --- TRASA DROGOWA ---
                        if iata in trasy_dojazdu and trasy_dojazdu[iata]:
                            route_geom_4326 = gpd.GeoSeries([trasy_dojazdu[iata]], crs="EPSG:2180").to_crs("EPSG:4326").iloc[0]
                            route_coords = [(y, x) for x, y in route_geom_4326.coords]
                            
                            folium.PolyLine(locations=[(miasto_wgs.y, miasto_wgs.x), route_coords[0]], color='gray', weight=2, dash_array='5, 5').add_to(m)
                            folium.PolyLine(locations=route_coords, color='#3388ff', weight=4, opacity=0.9, tooltip=f"Trasa główna do {iata}").add_to(m)
                            folium.PolyLine(locations=[route_coords[-1], (pt_lot.y, pt_lot.x)], color='gray', weight=2, dash_array='5, 5').add_to(m)
                        else:
                            folium.PolyLine(locations=[(miasto_wgs.y, miasto_wgs.x), (pt_lot.y, pt_lot.x)], color='red', weight=2, opacity=0.5, dash_array='5, 5').add_to(m)

                        # --- NOWOŚĆ: TRASA LOTU ---
                        trasa_lotu = dostepne_loty[(dostepne_loty['Origin'] == iata)]
                        if not trasa_lotu.empty:
                            geom_lotu = trasa_lotu.geometry.iloc[0]
                            # Rzutowanie w locie na siatkę mapy Folium (WGS84)
                            geom_lotu_wgs = gpd.GeoSeries([geom_lotu], crs=trasy.crs).to_crs("EPSG:4326").iloc[0]
                            coords_lotu = [(y, x) for x, y in geom_lotu_wgs.coords]
                            
                            # Rysowanie fioletowej linii symbolizującej lot
                            folium.PolyLine(
                                locations=coords_lotu,
                                color='purple', weight=3, opacity=0.7, dash_array='10, 10',
                                tooltip=f"Lot: {iata} ➔ {cel_iata}"
                            ).add_to(m)
                            
                            # 3. MARKER: Cel podroży (Cel lotu na końcu fioletowej ścieżki)
                            folium.Marker(
                                location=coords_lotu[-1], 
                                popup=f"Cel: {pelna_nazwa_celu}", 
                                icon=folium.Icon(color="red", icon="star")
                            ).add_to(m)

                st_folium(m, width=1200, height=600)