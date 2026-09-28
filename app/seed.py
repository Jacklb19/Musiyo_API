from app.db import Punto, Sala, make_session_factory


def main():
    factory = make_session_factory()
    with factory() as db:
        if db.get(Sala, "sala-prueba") is not None:
            return
        db.add(Sala(id="sala-prueba", recorrido_id="recorrido-prueba", orden=0))
        for orden in range(3):
            db.add(Punto(id=f"punto-0{orden + 1}", sala_id="sala-prueba", orden=orden))
        db.commit()


if __name__ == "__main__":
    main()
